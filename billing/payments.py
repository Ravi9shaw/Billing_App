"""Local UPI payment links; displaying a QR never confirms receipt of funds."""

import re
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import urlencode, quote

import qrcode


def valid_upi_id(value):
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,200}@[A-Za-z0-9.-]{2,64}", value))


def invoice_payment(bill, company):
    address = (company.get("upi_id") or "").strip()
    if not address or not valid_upi_id(address):
        return None
    try:
        amount = Decimal(str(bill.get("balance", 0)))
        if not amount.is_finite() or amount <= 0:
            return None
        amount = amount.quantize(Decimal("0.01"))
        if amount <= 0:
            return None
    except InvalidOperation:
        return None
    uri = "upi://pay?" + urlencode(
        {
            "pa": address,
            "pn": company["name"],
            "am": f"{amount:.2f}",
            "cu": "INR",
            "tn": "Invoice " + bill["bill_no"],
        },
        quote_via=quote,
    )
    image = qrcode.make(uri)
    content = BytesIO()
    image.save(content, format="PNG")
    return {
        "uri": uri,
        "png": content.getvalue(),
        "address": address,
        "amount": f"{amount:.2f}",
    }
