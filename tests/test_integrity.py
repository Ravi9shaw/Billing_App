import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from billing.database import Database, Conflict
from billing.money import calculate
from .conftest import payload, key


def test_tax_modes_and_rounding():
    lines = [{"rate": "105", "quantity": 1, "gst_rate": 5, "discount": 0}]
    assert calculate(lines, "inclusive")["taxable_amount"] == 100
    assert calculate(lines, "inclusive")["total"] == 105
    assert calculate(lines, "exclusive")["total"] == 110.25
    assert calculate(lines, "none")["total"] == 105
    q = calculate([{"rate": 0.1, "quantity": 3, "gst_rate": 5, "discount": 0}])
    assert q["total"] == 0.32 and q["cgst"] + q["sgst"] == q["gst_amount"]
    assert calculate(lines, "inclusive", True)["igst"] == 5
    for v in ["nan", "inf", -1]:
        with pytest.raises(ValueError):
            calculate([{"rate": v, "quantity": 1}])
    with pytest.raises(ValueError):
        calculate([{"rate": 10, "quantity": 0}])


def test_unpaid_bill_survives_restart(db, settings, item):
    bid = db.create_bill(payload(db, item, paid_now=0), "employee", settings, key())[
        "bill_id"
    ]
    reopened = Database(settings.db_path)
    bill, _ = reopened.get_bill(bid)
    assert bill["paid_amount"] == 0 and bill["balance"] == 105


def test_bills_concurrent_idempotency_stock_and_sequences(db, settings, item):
    body = payload(db, item)
    req = key()
    with ThreadPoolExecutor(8) as pool:
        duplicate = list(
            pool.map(
                lambda _: db.create_bill(body, "employee", settings, req), range(12)
            )
        )
    assert len({b["bill_id"] for b in duplicate}) == 1
    assert db.get_item_by_id(item)["stock_qty"] == 19
    with ThreadPoolExecutor(8) as pool:
        bills = list(
            pool.map(
                lambda _: db.create_bill(body, "employee", settings, key()), range(12)
            )
        )
    assert len({b["bill_no"] for b in bills}) == 12
    assert db.get_item_by_id(item)["stock_qty"] == 7
    last = max(bills, key=lambda b: b["bill_id"])
    old = db.get_bill(last["bill_id"])[0]
    db.void_bill(last["bill_id"], old["version"], "Mistake")
    new = db.create_bill(body, "employee", settings, key())
    assert new["bill_no"] != old["bill_no"]


def test_stock_round_trip_and_concurrent_payments(db, settings, item):
    with db._conn(write=True) as c:
        c.execute("UPDATE items SET stock_qty=0 WHERE id=?", (item,))
    body = payload(db, item, paid_now=0)
    bid = db.create_bill(body, "employee", settings, key())["bill_id"]
    db.void_bill(bid, 1, "Cancel")
    assert db.get_item_by_id(item)["stock_qty"] == 0
    bid = db.create_bill(body, "employee", settings, key())["bill_id"]

    def pay(_):
        try:
            return db.receive_payment(bid, {"amount": 80}, key())
        except Conflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(pay, range(2)))
    assert len([x for x in results if x]) == 1
    b, _ = db.get_bill(bid)
    assert b["paid_amount"] == 80 and b["balance"] == 25


def test_payment_retry_and_stale_edit(db, settings, item):
    bid = db.create_bill(payload(db, item, paid_now=0), "employee", settings, key())[
        "bill_id"
    ]
    k = key()
    assert db.receive_payment(bid, {"amount": 20}, k) == db.receive_payment(
        bid, {"amount": 20}, k
    )
    b, _ = db.get_bill(bid)
    assert b["paid_amount"] == 20
    with pytest.raises(Conflict):
        db.receive_payment(bid, {"amount": 21}, k)
    row = dict(db.get_item_by_id(item))
    body = {**row, "expected_stock": 20, "stock_qty": 20}
    with pytest.raises(Conflict):
        db.catalog_write("items", body, item)
    body["stock_qty"] = 19
    body["expected_stock"] = 19
    body["rate"] = 120
    db.catalog_write("items", body, item)
    with pytest.raises(Conflict):
        db.catalog_write("items", body, item)


def test_atomic_rollback_and_preserved_optional_fields(db, settings, item):
    for name in ["Alice", "Bob"]:
        db.catalog_write(
            "customers", {"name": name, "phone": "", "address": "", "notes": ""}
        )
    for name in ["A", "B"]:
        db.catalog_write(
            "items",
            {
                "name": name,
                "category_id": 1,
                "barcode": "",
                "size": "",
                "color": "",
                "rate": 0,
            },
        )
    body = payload(db, item)
    body["items"].append({**body["items"][0], "quantity": 0})
    with pytest.raises(ValueError):
        db.create_bill(body, "employee", settings, key())
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM bills").fetchone()[0] == 0
    assert db.get_item_by_id(item)["stock_qty"] == 20
    b = db.create_bill(payload(db, item), "employee", settings, key())
    assert db.get_bill(b["bill_id"])[0]["customer_id"] is None


def test_receipt_snapshot_and_admin_edit(db, settings, item):
    cid = db.catalog_write(
        "customers",
        {"name": "Original customer", "address": "Original address", "phone": ""},
    )
    body = payload(db, item, customer_id=cid, paid_now=20)
    bid = db.create_bill(body, "employee", settings, key())["bill_id"]
    with db._conn(write=True) as c:
        c.execute(
            "UPDATE customers SET name='Changed',address='Changed' WHERE id=?", (cid,)
        )
    before, _ = db.get_bill(bid)
    assert before["customer_name"] == "Original customer"
    body["version"] = before["version"]
    body["items"][0]["quantity"] = 2
    db.create_bill(body, "admin", settings, key(), edit_id=bid)
    b, lines = db.get_bill(bid)
    assert (
        b["paid_amount"] == 20
        and b["total"] == 210
        and db.get_item_by_id(item)["stock_qty"] == 18
    )
    with pytest.raises(Conflict):
        db.create_bill(body, "admin", settings, key(), edit_id=bid)
    with db._conn() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action='edit-bill-before'"
            ).fetchone()[0]
            == 1
        )


def test_migration_existing_payment_schema_does_not_guess(tmp_path):
    from billing.queries import SCHEMA

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.execute(
            "CREATE TABLE payments(id INTEGER PRIMARY KEY,bill_id INTEGER,customer_id INTEGER,payment_date TEXT,amount REAL,payment_mode TEXT,notes TEXT)"
        )
        c.execute(
            "INSERT INTO customers(name,address) VALUES('Old customer','Old address')"
        )
        c.execute(
            "INSERT INTO bills(bill_no,customer_id,bill_date,subtotal,total) VALUES('INV-20200101-005',1,'2020-01-01',100,100)"
        )
    db = Database(path)
    assert db.get_bill(1)[0]["balance"] == 100
    assert len(list((tmp_path / "backups").glob("*before-migration.db"))) == 1
    Database(path)
    assert db.get_bill(1)[0]["balance"] == 100
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            json.loads(c.execute("SELECT customer_snapshot FROM bills").fetchone()[0])[
                "address"
            ]
            == "Old address"
        )


def test_legacy_without_payments_migrates_once(tmp_path):
    from billing.queries import SCHEMA

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
        c.execute(
            "INSERT INTO bills(bill_no,bill_date,subtotal,total) VALUES('INV-20200101-001','2020-01-01',100,100)"
        )
    d = Database(path)
    Database(path)
    b, _ = d.get_bill(1)
    assert b["paid_amount"] == 100
    with d._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1


def test_backup_contains_committed_data(db, settings, item, tmp_path):
    bill = db.create_bill(payload(db, item), "employee", settings, key())
    path = db.backup_database()
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT bill_no FROM bills").fetchone()[0] == bill["bill_no"]


def test_process_crash_rolls_back_only_uncommitted_work(db, settings, item):
    import subprocess
    import sys

    body = payload(db, item, paid_now=25)
    script = """
import json, os, sys
from billing.config import Settings
from billing.database import Database
settings = Settings()
db = Database(settings.db_path)
db.create_bill(json.loads(sys.argv[1]), "employee", settings, "committed-before-crash")
with db._conn(write=True) as c:
    c.execute("UPDATE items SET stock_qty=999")
    c.execute("UPDATE bills SET total=999")
    c.execute("DELETE FROM payments")
    os._exit(19)  # No Python cleanup, rollback, or graceful server shutdown.
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(body)], timeout=30
    )
    assert result.returncode == 19
    reopened = Database(settings.db_path)
    with reopened._conn() as c:
        bid = c.execute("SELECT id FROM bills").fetchone()[0]
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    bill, _ = reopened.get_bill(bid)
    assert bill["total"] == 105
    assert bill["paid_amount"] == 25 and bill["balance"] == 80
    assert reopened.get_item_by_id(item)["stock_qty"] == 19
    assert (
        reopened.create_bill(body, "employee", settings, "committed-before-crash")[
            "bill_id"
        ]
        == bid
    )


def test_catalog_retry_does_not_duplicate_or_overwrite(db, item):
    body = {"name": "Retry customer", "phone": "", "address": ""}
    request = key()
    first = db.catalog_write("customers", body, request_key=request)
    assert db.catalog_write("customers", body, request_key=request) == first
    with pytest.raises(Conflict):
        db.catalog_write(
            "customers", {**body, "name": "Different"}, request_key=request
        )
