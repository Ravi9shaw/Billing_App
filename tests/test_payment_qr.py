from io import BytesIO
from urllib.parse import urlsplit, parse_qs

import pytest
from PIL import Image

from billing.invoice import render_html, render_pdf
from billing.payments import invoice_payment
from billing.setup import save_settings
from .conftest import payload, key


def test_payment_qr_uses_saved_recipient_and_actual_balance(db, settings, item):
    settings.company.update(upi_id="synthetic.shop@bank", name="Synthetic & Shop")
    bid = db.create_bill(payload(db, item, paid_now=20), "employee", settings, key())[
        "bill_id"
    ]
    settings.company["upi_id"] = "different@bank"
    bill, lines = db.get_bill(bid)
    payment = invoice_payment(bill, bill["company"])
    uri = urlsplit(payment["uri"])
    assert (uri.scheme, uri.netloc) == ("upi", "pay")
    assert parse_qs(uri.query) == {
        "pa": ["synthetic.shop@bank"],
        "pn": ["Synthetic & Shop"],
        "am": ["85.00"],
        "cu": ["INR"],
        "tn": ["Invoice " + bill["bill_no"]],
    }
    Image.open(BytesIO(payment["png"])).verify()
    html = render_html(bill, lines, settings.company)
    assert 'alt="UPI payment QR"' in html and "Scan to pay INR 85.00" in html
    assert "synthetic.shop@bank" in html and "different@bank" not in html
    pdf = render_pdf(bill, lines, settings.company)
    assert b"/Subtype /Image" in pdf
    assert pdf.count(b"/Type /Page\n") == 1
    assert db.get_bill(bid)[0]["paid_amount"] == 20
    db.receive_payment(bid, {"amount": 85}, key())
    paid, lines = db.get_bill(bid)
    assert invoice_payment(paid, paid["company"]) is None
    assert 'alt="UPI payment QR"' not in render_html(paid, lines, settings.company)


@pytest.mark.parametrize(
    "address,balance",
    [
        ("", 10),
        ("9000000000", 10),
        ("bad@bank&pa=other@bank", 10),
        ("ok@bank", 0),
        ("ok@bank", -1),
        ("ok@bank", "NaN"),
    ],
)
def test_payment_qr_omitted_without_valid_recipient_or_balance(address, balance):
    assert (
        invoice_payment(
            {"balance": balance, "bill_no": "INV-TEST"},
            {"upi_id": address, "name": "Test"},
        )
        is None
    )


def test_setup_rejects_bare_phone_without_replacing_configuration(settings):
    path = settings.data_dir / ".env"
    path.write_text("COMPANY_NAME=Existing\n")
    with pytest.raises(ValueError, match="UPI ID"):
        save_settings({"UPI_ID": "9000000000"}, settings)
    assert path.read_text() == "COMPANY_NAME=Existing\n"
