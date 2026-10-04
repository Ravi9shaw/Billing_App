import json
import sqlite3

import httpx
import pytest
from billing.lock import server_lock
from billing.maintenance import restore_backup
from billing.invoice import render_html, render_pdf
from billing.whatsapp import MetaProvider, enqueue_bill, enqueue_campaign, worker
from .conftest import payload, key


def test_restore_is_offline_and_keeps_safety_copy(db, settings, item):
    bid = db.create_bill(payload(db, item), "employee", settings, key())["bill_id"]
    backup = db.backup_database(reason="manual")
    db.create_bill(payload(db, item), "employee", settings, key())
    with server_lock(settings.db_path):
        with pytest.raises(RuntimeError):
            restore_backup(settings, backup)
    safety = restore_backup(settings, backup)
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 1
    with sqlite3.connect(safety) as c:
        assert c.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 2
    assert db.get_bill(bid)[0]


def test_html_pdf_use_saved_data_and_paginate(db, settings, item):
    cid = db.catalog_write(
        "customers",
        {"name": "<script>alert(1)</script>", "address": "Optional address"},
    )
    body = payload(db, item, customer_id=cid, paid_now=20, tax_mode="inclusive")
    body["items"] *= 60
    bid = db.create_bill(body, "admin", settings, key())["bill_id"]
    b, lines = db.get_bill(bid)
    html = render_html(b, lines, settings.company)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Optional address" in html and "5,980.00" in html
    pdf = render_pdf(b, lines, settings.company)
    assert pdf.startswith(b"%PDF")
    # ReportLab emits explicit Page objects for each page.
    assert pdf.count(b"/Type /Page\n") >= 2


def enabled(settings):
    settings.whatsapp_provider = "meta"
    settings.whatsapp_token = "synthetic-token"
    settings.whatsapp_phone_id = "123"
    settings.whatsapp_bill_template = "invoice_document"


def test_whatsapp_queue_requires_consent_and_is_idempotent(db, settings, item):
    enabled(settings)
    cid = db.catalog_write(
        "customers", {"name": "Synthetic recipient", "phone": "+919000000001"}
    )
    bid = db.create_bill(
        payload(db, item, customer_id=cid), "employee", settings, key()
    )["bill_id"]
    with pytest.raises(ValueError):
        enqueue_bill(db, settings, bid, {}, key())
    with db._conn(write=True) as c:
        c.execute("UPDATE customers SET whatsapp_opt_in=1 WHERE id=?", (cid,))
    k = key()
    first = enqueue_bill(db, settings, bid, {}, k)
    assert enqueue_bill(db, settings, bid, {}, k) == first
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1


def test_meta_request_contract(settings):
    enabled(settings)
    provider = MetaProvider(settings)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "media-id"})
        body = json.loads(request.content)
        assert body["messaging_product"] == "whatsapp"
        assert (
            body["template"]["components"][0]["parameters"][0]["document"]["id"]
            == "media-id"
        )
        assert body["template"]["components"][1]["parameters"][0]["text"] == "INV-TEST"
        return httpx.Response(200, json={"messages": [{"id": "message-id"}]})

    provider.client.close()
    provider.client = httpx.Client(transport=httpx.MockTransport(handler))
    media = provider.upload(b"%PDF-synthetic", "test.pdf", "application/pdf")
    assert (
        provider.send(
            "919000000001",
            "invoice_document",
            "en",
            media,
            "document",
            "INV-TEST",
            "test.pdf",
        )
        == "message-id"
    )
    assert len(requests) == 2
    provider.close()


class StopAfterOne:
    def is_set(self):
        return False

    def __init__(self):
        self.calls = 0

    def wait(self, _):
        self.calls += 1
        return self.calls > 1


def test_worker_withdrawal_and_ambiguous_send_are_not_retried(
    db, settings, item, monkeypatch
):
    enabled(settings)
    cid = db.catalog_write(
        "customers",
        {"name": "Synthetic", "phone": "919000000001", "whatsapp_opt_in": 1},
    )
    bid = db.create_bill(
        payload(db, item, customer_id=cid), "employee", settings, key()
    )["bill_id"]
    enqueue_bill(db, settings, bid, {}, key())

    class FakeProvider:
        sends = 0

        def __init__(self, _):
            pass

        def close(self):
            pass

        def upload(self, *a):
            return "media"

        def send(self, *a):
            FakeProvider.sends += 1
            raise httpx.ReadTimeout("Simulated lost response")

    monkeypatch.setattr("billing.whatsapp.MetaProvider", FakeProvider)
    worker(db, settings, StopAfterOne())
    worker(db, settings, StopAfterOne())
    assert FakeProvider.sends == 1
    with db._conn() as c:
        assert c.execute("SELECT status FROM outbox").fetchone()[0] == "uncertain"
    enqueue_bill(db, settings, bid, {}, key())
    with db._conn(write=True) as c:
        c.execute("UPDATE customers SET whatsapp_opt_in=0 WHERE id=?", (cid,))
    worker(db, settings, StopAfterOne())
    with db._conn() as c:
        assert (
            c.execute("SELECT status FROM outbox ORDER BY id DESC").fetchone()[0]
            == "skipped"
        )
    assert FakeProvider.sends == 1


def test_campaign_only_includes_eligible_customers(db, settings, tmp_path):
    enabled(settings)
    db.catalog_write(
        "customers",
        {"name": "Consented", "phone": "919000000001", "whatsapp_opt_in": 1},
    )
    db.catalog_write("customers", {"name": "No permission", "phone": "919000000002"})
    db.catalog_write(
        "customers", {"name": "Invalid phone", "phone": "12", "whatsapp_opt_in": 1}
    )
    folder = settings.data_dir / "uploads"
    folder.mkdir()
    identifier = "a" * 32 + ".png"
    (folder / identifier).write_bytes(b"synthetic")
    body = {"media": identifier, "template": "new_stock", "text": "New stock arrived"}
    k = key()
    result = enqueue_campaign(db, settings, body, k)
    assert result["queued"] == 1 and result["invalid_phones_skipped"] == 1
    assert enqueue_campaign(db, settings, body, k) == result


def test_setup_preserves_existing_records_and_optional_settings(
    db, settings, item, monkeypatch
):
    from billing import setup
    from billing.config import verify_password

    bid = db.create_bill(payload(db, item, paid_now=0), "employee", settings, key())[
        "bill_id"
    ]
    env_file = settings.data_dir / ".env"
    env_file.write_text("BANK_NAME=Existing Bank\nCOMPANY_ADDRESS=\n", encoding="utf-8")
    answers = iter(["Configured Shop", "9000000000", "synthetic@bank", "disabled", "n"])
    passwords = iter(["new-test-password", "new-test-password"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(setup.getpass, "getpass", lambda _: next(passwords))
    setup.main()
    saved = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert saved["COMPANY_NAME"] == "Configured Shop"
    assert saved["UPI_ID"] == "synthetic@bank"
    assert saved["BANK_NAME"] == "Existing Bank" and saved["COMPANY_ADDRESS"] == ""
    assert verify_password("new-test-password", saved["ADMIN_PASSWORD_HASH"])
    assert "new-test-password" not in env_file.read_text()
    bill, _ = db.get_bill(bid)
    assert bill["total"] == 105 and bill["paid_amount"] == 0
