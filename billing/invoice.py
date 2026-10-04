"""Invoice layout follows the supplied printed bill; all values come from saved snapshots."""

import base64
from .payments import invoice_payment
from html import escape
from io import BytesIO
from decimal import Decimal


def amount_words(value):
    ones = [
        "Zero",
        "One",
        "Two",
        "Three",
        "Four",
        "Five",
        "Six",
        "Seven",
        "Eight",
        "Nine",
        "Ten",
        "Eleven",
        "Twelve",
        "Thirteen",
        "Fourteen",
        "Fifteen",
        "Sixteen",
        "Seventeen",
        "Eighteen",
        "Nineteen",
    ]
    tens = [
        "",
        "",
        "Twenty",
        "Thirty",
        "Forty",
        "Fifty",
        "Sixty",
        "Seventy",
        "Eighty",
        "Ninety",
    ]

    def words(n):
        if n < 20:
            return ones[n]
        if n < 100:
            return tens[n // 10] + (" " + words(n % 10) if n % 10 else "")
        for unit, label in [
            (10000000, "Crore"),
            (100000, "Lakh"),
            (1000, "Thousand"),
            (100, "Hundred"),
        ]:
            if n >= unit:
                return (
                    words(n // unit)
                    + " "
                    + label
                    + (" " + words(n % unit) if n % unit else "")
                )

    paise = int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    return (
        words(paise // 100)
        + " Rupees"
        + (" and " + words(paise % 100) + " Paise" if paise % 100 else "")
        + " Only"
    )


def fmt(v):
    return f"{float(v or 0):,.2f}"


def esc(v):
    return escape(str(v or ""))


def data(b, items, fallback):
    company = b.get("company") or fallback
    meta = b.get("metadata") or {}
    legacy = b.get("tax_mode") == "legacy"
    taxes = [
        ("CGST", b.get("cgst", 0)),
        ("SGST", b.get("sgst", 0)),
        ("IGST", b.get("igst", 0)),
    ]
    if legacy:
        taxes = [("Recorded GST", b.get("gst_amount", 0))]
    else:
        rates = {float(i.get("gst_rate", 0)) for i in items}
        rate = next(iter(rates)) if len(rates) == 1 else None
        taxes = [
            (
                label
                + (
                    f" ({rate if label == 'IGST' else rate / 2:g}%)"
                    if rate is not None
                    else " (mixed rates)"
                ),
                value,
            )
            for label, value in taxes
        ]
    summary = (
        [
            ("Total amount", b["subtotal"]),
            ("Discount", b["discount_amount"]),
            ("Taxable amount", b.get("taxable_amount")),
        ]
        + taxes
        + [
            ("Grand total", b["total"]),
            ("Paid", b.get("paid_amount", 0)),
            ("Balance", b.get("balance", 0)),
        ]
    )
    return company, meta, summary, legacy


def render_html(b, items, fallback):
    c, m, summary, legacy = data(b, items, fallback)
    rows = "".join(
        f'<tr><td>{i}</td><td class="particular">{esc(r["item_name_snapshot"])}</td><td>{esc(r.get("hsn"))}</td><td>{r["quantity"]}</td><td>{fmt(r["rate"])}</td><td>{fmt(r["quantity"] * r["rate"])}</td></tr>'
        for i, r in enumerate(items, 1)
    )
    blank = "".join(
        '<tr class="blank"><td>&nbsp;</td><td></td><td></td><td></td><td></td><td></td></tr>'
        for _ in range(max(0, 12 - len(items)))
    )
    summary_html = "".join(
        f"<tr><th>{esc(label)}</th><td>{'Not recorded' if value is None else fmt(value)}</td></tr>"
        for label, value in summary
    )
    payment = invoice_payment(b, c)
    payment_html = ""
    if payment:
        image = base64.b64encode(payment["png"]).decode("ascii")
        payment_html = f'<div style="float:right;width:132px;text-align:center;margin:0 0 8px 8px;overflow-wrap:anywhere"><b>Scan to pay INR {payment["amount"]}</b><br><a href="{esc(payment["uri"])}"><img alt="UPI payment QR" width="124" height="124" src="data:image/png;base64,{image}"></a><br>{esc(payment["address"])}<br><small>Confirm recipient in your UPI app. Record payment after receipt.</small></div>'
    title = "INVOICE" if b.get("tax_mode") == "none" else "TAX INVOICE"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(b["bill_no"])}</title><style>
    @page{{size:A4;margin:10mm}}*{{box-sizing:border-box}}body{{font:12px Arial,sans-serif;color:#182540;margin:0;background:white}}.sheet{{max-width:190mm;margin:auto;border:2px solid #243550}}header{{text-align:center;border-bottom:1px solid;padding:8px}}.top{{display:flex;justify-content:space-between;font-size:11px}}h1{{font: bold 29px Georgia,serif;margin:8px 0}}p{{margin:4px 0}}table{{width:100%;border-collapse:collapse;table-layout:fixed}}th,td{{border:1px solid #34415a;padding:6px;vertical-align:top;overflow-wrap:anywhere}}th{{background:#f1f3f7}}.customer td{{height:30px}}.items th:nth-child(1){{width:6%}}.items th:nth-child(2){{width:42%}}.items th:nth-child(3){{width:13%}}.items td{{text-align:right;border-top:0;border-bottom:0;height:25px}}.items td.particular{{text-align:left}}.items .blank td{{height:24px}}.bottom{{display:grid;grid-template-columns:63% 37%;border-top:1px solid}}.details{{padding:8px;border-right:1px solid}}.summary th{{text-align:left;font-size:11px}}.summary td{{text-align:right}}.signatures{{display:grid;grid-template-columns:42% 23% 35%;border-top:1px solid;min-height:90px}}.signatures>div{{padding:8px;border-right:1px solid}}.signature{{display:flex;flex-direction:column;justify-content:space-between;text-align:center}}.note{{font-size:10px;padding:5px}}.actions{{margin:12px;text-align:center}}button{{padding:8px 18px}}@media print{{.actions{{display:none}}.bottom,.signatures{{break-inside:avoid}}thead{{display:table-header-group}}tr{{break-inside:avoid}}}} </style></head><body>
    <div class="actions"><button onclick="window.print()">Print invoice</button></div><article class="sheet">
    <header><div class="top"><span>GSTIN: {esc(c.get("gstin")) or "—"}</span><b>{title}</b><span>Phone: {esc(c.get("phone")) or "—"}</span></div><h1>{esc(c["name"])}</h1><p>{esc(c.get("address"))}</p><p>{esc(c.get("email"))}</p></header>
    <table class="customer"><tr><td colspan="2">Book No.: {esc(m.get("book_no")) or "—"}</td><td>Invoice No.: <b>{esc(b["bill_no"])}</b></td><td>Date: {esc(b["bill_date"][:10])}</td></tr>
    <tr><td colspan="2" rowspan="3"><p>M/s: <b>{esc(b.get("customer_name")) or "Walk-in customer"}</b></p><p>Address: {esc(b.get("customer_address")) or "—"}</p><p>Pin Code: {esc(m.get("pin_code")) or "—"} &nbsp; Mob.: {esc(b.get("customer_phone")) or "—"}</p></td><td>E-Way Bill No.: {esc(m.get("eway_no"))}</td><td>Date: {esc(m.get("eway_date"))}</td></tr><tr><td>P.O. No.: {esc(m.get("po_no"))}</td><td>Date: {esc(m.get("po_date"))}</td></tr><tr><td>TPT: {esc(m.get("transport"))}</td><td>L/R: {esc(m.get("lr_no"))}</td></tr></table>
    <table class="items"><thead><tr><th>Sl. No.</th><th>Particulars</th><th>HSN Code</th><th>Qty.</th><th>Per Rate</th><th>Amount</th></tr></thead><tbody>{rows}{blank}</tbody></table>
    <div class="bottom"><div class="details"><p><b>Party’s GSTIN:</b> {esc(m.get("party_gstin")) or "—"}</p><p><b>Total invoice amount in words:</b><br>{esc(amount_words(b["total"]))}</p><hr>{payment_html}<p><b>BANK DETAILS</b></p><p>Bank name: {esc(c.get("bank_name")) or "—"}</p><p>A/C No.: {esc(c.get("bank_account")) or "—"}</p><p>IFSC: {esc(c.get("bank_ifsc")) or "—"}</p><p>Branch: {esc(c.get("bank_branch")) or "—"}</p><p>Payment mode: {esc(b.get("payment_mode"))}</p><p>GST: {esc(b.get("tax_mode"))}</p></div><table class="summary">{summary_html}</table></div>
    {'<div class="note">Historical invoice: the original tax and stock details require verification; recorded totals are preserved.</div>' if legacy else ""}
    <div class="signatures"><div><b>Terms &amp; Conditions</b><p>{esc(c.get("terms"))}</p></div><div class="signature"><span></span><b>Customer Seal &amp; Sign.</b></div><div class="signature"><b>For {esc(c["name"])}</b><span>Authorised Signatory</span></div></div></article></body></html>"""


def render_pdf(b, items, fallback):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Table,
        TableStyle,
        Spacer,
        Image,
    )

    c, m, summary, legacy = data(b, items, fallback)
    buffer = BytesIO()
    styles = getSampleStyleSheet()
    normal = ParagraphStyle("invoice", parent=styles["Normal"], fontSize=9, leading=12)

    def p(v, bold=False):
        return Paragraph(
            ("<b>" + esc(v) + "</b>") if bold else esc(v).replace("\n", "<br/>"), normal
        )

    width = A4[0] - 48
    grid = [
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#34415a")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]

    def table(rows, widths, extra=[]):
        t = Table(rows, colWidths=widths)
        t.setStyle(TableStyle(grid + extra))
        return t

    story = [
        table(
            [
                [
                    p("GSTIN: " + c.get("gstin", "")),
                    p(
                        "INVOICE" if b.get("tax_mode") == "none" else "TAX INVOICE",
                        True,
                    ),
                    p("Phone: " + c.get("phone", "")),
                ]
            ],
            [width / 3] * 3,
        ),
        Paragraph(
            esc(c["name"]),
            ParagraphStyle(
                "company", fontName="Times-Bold", fontSize=23, leading=28, alignment=1
            ),
        ),
        p(c.get("address", "")),
        p(c.get("email", "")),
        Spacer(1, 8),
    ]
    story.append(
        table(
            [
                [
                    p("Book No.: " + m.get("book_no", "")),
                    p("Invoice No.: " + b["bill_no"], True),
                    p("Date: " + b["bill_date"][:10]),
                ],
                [
                    p(
                        "M/s: "
                        + (b.get("customer_name") or "Walk-in customer")
                        + "\nAddress: "
                        + (b.get("customer_address") or "")
                        + "\nPin: "
                        + m.get("pin_code", "")
                        + "   Mob.: "
                        + (b.get("customer_phone") or "")
                    ),
                    p(
                        "E-Way Bill: "
                        + m.get("eway_no", "")
                        + "\nP.O. No.: "
                        + m.get("po_no", "")
                        + "\nTPT: "
                        + m.get("transport", "")
                    ),
                    p(
                        "Date: "
                        + m.get("eway_date", "")
                        + "\nDate: "
                        + m.get("po_date", "")
                        + "\nL/R: "
                        + m.get("lr_no", "")
                    ),
                ],
            ],
            [width * 0.52, width * 0.28, width * 0.2],
        )
    )
    rows = [
        [
            p(v, True)
            for v in [
                "Sl. No.",
                "Particulars",
                "HSN Code",
                "Qty.",
                "Per Rate",
                "Amount",
            ]
        ]
    ]
    rows.extend(
        [
            [
                p(str(i)),
                p(r["item_name_snapshot"]),
                p(r.get("hsn") or ""),
                p(str(r["quantity"])),
                p(fmt(r["rate"])),
                p(fmt(r["quantity"] * r["rate"])),
            ]
            for i, r in enumerate(items, 1)
        ]
    )
    rows.extend([[p(" ")] * 6 for _ in range(max(0, 12 - len(items)))])
    t = Table(
        rows,
        colWidths=[width * x for x in [0.06, 0.42, 0.13, 0.08, 0.14, 0.17]],
        repeatRows=1,
    )
    t.setStyle(
        TableStyle(grid + [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f3f7"))])
    )
    story.append(t)
    bank = (
        "Party GSTIN: "
        + m.get("party_gstin", "")
        + "\n\nTotal invoice amount in words:\n"
        + amount_words(b["total"])
        + "\n\nBANK DETAILS\nBank: "
        + c.get("bank_name", "")
        + "\nA/C: "
        + c.get("bank_account", "")
        + "\nIFSC: "
        + c.get("bank_ifsc", "")
        + "\nBranch: "
        + c.get("bank_branch", "")
        + "\nPayment: "
        + (b.get("payment_mode") or "")
        + "\nGST: "
        + b.get("tax_mode", "")
    )
    totals = table(
        [
            [
                p(label, label == "Grand total"),
                p("Not recorded" if v is None else fmt(v), label == "Grand total"),
            ]
            for label, v in summary
        ],
        [width * 0.23, width * 0.14],
    )
    payment = invoice_payment(b, c)
    bank_content = p(bank)
    if payment:
        qr_content = [
            p("Scan to pay INR " + payment["amount"], True),
            Image(BytesIO(payment["png"]), width=104, height=104),
            p(payment["address"]),
            p("Confirm recipient in your UPI app. Record payment after receipt."),
        ]
        bank_content = Table(
            [[p(bank), qr_content]], colWidths=[width * 0.36 - 12, width * 0.27]
        )
        bank_content.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
    footer = table(
        [[bank_content, totals]],
        [width * 0.63, width * 0.37],
        [
            ("LEFTPADDING", (1, 0), (1, 0), 0),
            ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (1, 0), (1, 0), 0),
            ("BOTTOMPADDING", (1, 0), (1, 0), 0),
        ],
    )
    story.append(footer)
    if legacy:
        story.append(
            p(
                "Historical invoice: recorded amounts preserved; tax breakdown requires verification."
            )
        )
    story.append(
        table(
            [
                [
                    p("Terms & Conditions\n" + c.get("terms", "")),
                    p("\n\nCustomer Seal & Sign."),
                    p("For " + c["name"] + "\n\nAuthorised Signatory"),
                ]
            ],
            [width * 0.42, width * 0.23, width * 0.35],
        )
    )
    SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=24,
        leftMargin=24,
        topMargin=20,
        bottomMargin=20,
        title=b["bill_no"],
    ).build(story)
    return buffer.getvalue()
