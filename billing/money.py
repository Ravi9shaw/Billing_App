"""Decimal, per-line rounding is authoritative for all clients."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date

CENT = Decimal("0.01")


def decimal(value, name="Amount"):
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not value.is_finite() or value < 0 or value > Decimal("999999999"):
        raise ValueError(f"{name} must be finite and between 0 and 999999999")
    return value


def money(value):
    return decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def iso_date(value=None):
    value = value or date.today().isoformat()
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError("Use a valid YYYY-MM-DD date")
    if parsed > date.today():
        raise ValueError("Date cannot be in the future")
    return parsed.isoformat()


def calculate(lines, mode="exclusive", interstate=False):
    if mode not in ("exclusive", "inclusive", "none"):
        raise ValueError("Unknown GST mode")
    if not lines or len(lines) > 500:
        raise ValueError("A bill needs 1 to 500 items")
    result = []
    for line in lines:
        qty = line.get("quantity", 1)
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 100000:
            raise ValueError("Quantity must be a positive whole number")
        rate = money(line["rate"])
        gross = money(rate * qty)
        discount = money(line.get("discount", 0))
        if discount > gross:
            raise ValueError("Discount cannot exceed the line amount")
        gst = (
            decimal(line.get("gst_rate", 0), "GST rate")
            if mode != "none"
            else Decimal(0)
        )
        if gst > 100:
            raise ValueError("GST rate cannot exceed 100")
        net = gross - discount
        taxable = money(net / (1 + gst / 100)) if mode == "inclusive" else net
        tax = net - taxable if mode == "inclusive" else money(taxable * gst / 100)
        cgst = Decimal(0) if interstate else money(tax / 2)
        sgst = Decimal(0) if interstate else tax - cgst
        result.append(
            {
                **line,
                "quantity": qty,
                "rate": float(rate),
                "gross": float(gross),
                "discount": float(discount),
                "subtotal": float(taxable),
                "gst_rate": float(gst),
                "gst_amount": float(tax),
                "cgst": float(cgst),
                "sgst": float(sgst),
                "igst": float(tax if interstate else 0),
                "line_total": float(taxable + tax),
            }
        )

    def total(key):
        return float(sum((Decimal(str(r[key])) for r in result), Decimal(0)))

    return dict(
        items=result,
        subtotal=total("gross"),
        discount_amount=total("discount"),
        taxable_amount=total("subtotal"),
        gst_amount=total("gst_amount"),
        cgst=total("cgst"),
        sgst=total("sgst"),
        igst=total("igst"),
        total=total("line_total"),
        tax_mode=mode,
        interstate=interstate,
    )
