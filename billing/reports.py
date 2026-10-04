"""Read-only reports share the exact predicates used for CSV downloads."""

import calendar
import csv
import io
from datetime import date, timedelta

PAGE_SIZE = 50


def period(q, today=None):
    today = today or date.today()
    choice = q.get("period", "all")
    start, end = None, today
    if choice == "today":
        start = today
    elif choice in ("7days", "30days"):
        start = today - timedelta(days=6 if choice == "7days" else 29)
    elif choice in ("month", "lastmonth", "3months"):
        end = today.replace(day=1) - timedelta(days=1)
        if choice == "month":
            year, month = map(
                int, (q.get("month") or today.strftime("%Y-%m")).split("-")
            )
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
        else:
            index = end.year * 12 + end.month - (3 if choice == "3months" else 1)
            start = date(index // 12, index % 12 + 1, 1)
    elif choice == "year":
        year = int(q.get("year") or today.year)
        start, end = date(year, 1, 1), date(year, 12, 31)
    elif choice == "custom":
        start = date.fromisoformat(q["date_from"]) if q.get("date_from") else None
        end = date.fromisoformat(q["date_to"]) if q.get("date_to") else today
    elif choice != "all":
        raise ValueError("Unknown reporting period")
    if start and start > end:
        raise ValueError("From date must be on or before To date")
    if end.year >= 9999:
        raise ValueError("Choose a year before 9999")
    return start, end


def date_where(column, bounds):
    start, end = bounds
    return (
        f"{column}>=? AND {column}<?",
        [
            start.isoformat() if start else "0001-01-01",
            (end + timedelta(days=1)).isoformat(),
        ],
    )


def like(value):
    return (
        "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    )


BILLS = """SELECT b.*, COALESCE(json_extract(b.customer_snapshot,'$.name'),c.name,'Walk-in') AS customer_name,
COALESCE(json_extract(b.customer_snapshot,'$.phone'),c.phone,'') AS customer_phone,
COALESCE(p.paid,0) AS paid_amount, MAX(b.total-COALESCE(p.paid,0),0) AS balance,
COALESCE((SELECT SUM(quantity) FROM bill_items WHERE bill_id=b.id),0) AS pieces
FROM bills b LEFT JOIN customers c ON c.id=b.customer_id
LEFT JOIN (SELECT bill_id,SUM(amount) AS paid FROM payments GROUP BY bill_id) p ON p.bill_id=b.id"""
BALANCES = """SELECT c.id,c.name,c.phone,c.address,COUNT(b.id) AS visits,
COALESCE(SUM(b.total),0) AS total_billed,COALESCE(SUM(b.paid_amount),0) AS total_paid,
COALESCE(SUM(b.balance),0) AS balance,MAX(b.bill_date) AS last_visit
FROM customers c LEFT JOIN sale b ON b.customer_id=c.id GROUP BY c.id"""


def query(kind, q):
    bounds = period(q)
    search = like(q.get("search", "").strip())
    if kind == "sales":
        clause, args = date_where("bill_date", bounds)
        sql = f"WITH sale AS ({BILLS}) SELECT * FROM sale WHERE {clause} AND (bill_no LIKE ? ESCAPE '\\' OR customer_name LIKE ? ESCAPE '\\' OR customer_phone LIKE ? ESCAPE '\\')"
        args += [search] * 3
        if q.get("customer_id"):
            sql += " AND customer_id=?"
            args.append(int(q["customer_id"]))
        return (
            sql,
            args,
            "bill_date DESC,id DESC",
            [
                "bill_no",
                "bill_date",
                "customer_name",
                "customer_phone",
                "pieces",
                "subtotal",
                "discount_amount",
                "total",
                "paid_amount",
                "balance",
            ],
            bounds,
        )
    if kind == "expenses":
        clause, args = date_where("expense_date", bounds)
        sql = f"SELECT * FROM expenses WHERE {clause} AND (category LIKE ? ESCAPE '\\' OR COALESCE(description,'') LIKE ? ESCAPE '\\')"
        args += [search, search]
        if q.get("category"):
            sql += " AND category=?"
            args.append(q["category"])
        return (
            sql,
            args,
            "expense_date DESC,id DESC",
            ["expense_date", "category", "amount", "payment_mode", "description"],
            bounds,
        )
    if kind == "balances":
        sql = f"WITH sale AS ({BILLS}), balances AS ({BALANCES}) SELECT * FROM balances WHERE (name LIKE ? ESCAPE '\\' OR COALESCE(phone,'') LIKE ? ESCAPE '\\')"
        if q.get("outstanding") == "1":
            sql += " AND balance>0.005"
        return (
            sql,
            [search, search],
            "name,id",
            [
                "name",
                "phone",
                "address",
                "visits",
                "total_billed",
                "total_paid",
                "balance",
            ],
            (None, date.today()),
        )
    raise ValueError("Unknown report")


def report(db, kind, q):
    sql, args, order, _, bounds = query(kind, q)
    offset = max(0, int(q.get("offset", 0)))
    totals = {
        "sales": "SUM(total) AS total,SUM(paid_amount) AS paid,SUM(balance) AS outstanding,SUM(pieces) AS pieces",
        "expenses": "SUM(amount) AS total",
        "balances": "SUM(total_billed) AS total,SUM(total_paid) AS paid,SUM(balance) AS outstanding",
    }[kind]
    with db._conn() as c:
        summary = dict(
            c.execute(
                f"SELECT COUNT(*) AS count,{totals} FROM ({sql})", args
            ).fetchone()
        )
        rows = [
            dict(r)
            for r in c.execute(
                sql + f" ORDER BY {order} LIMIT ? OFFSET ?", [*args, PAGE_SIZE, offset]
            )
        ]
        result = {
            "rows": rows,
            "summary": {k: v or 0 for k, v in summary.items()},
            "offset": offset,
            "limit": PAGE_SIZE,
            "date_from": str(bounds[0]) if bounds[0] else None,
            "date_to": str(bounds[1]),
        }
        if kind == "expenses":
            result["categories"] = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT category FROM expenses ORDER BY category"
                )
            ]
        if kind == "balances":
            result["overall"] = dict(
                c.execute(
                    f"WITH sale AS ({BILLS}), balances AS ({BALANCES}) SELECT COALESCE(SUM(total_billed),0) AS total,COALESCE(SUM(total_paid),0) AS paid,COALESCE(SUM(balance),0) AS outstanding FROM balances"
                ).fetchone()
            )
        return result


def export_csv(db, kind, q):
    sql, args, order, columns, _ = query(kind, q)

    def stream():
        out = io.StringIO()
        writer = csv.writer(out)
        yield "\ufeff"
        writer.writerow(columns)
        yield out.getvalue()
        out.seek(0)
        out.truncate(0)
        with db._conn() as c:
            for row in c.execute(sql + " ORDER BY " + order, args):
                writer.writerow(
                    [
                        ("'" + str(row[k]))
                        if isinstance(row[k], str)
                        and row[k].lstrip().startswith(("=", "+", "-", "@"))
                        else row[k]
                        for k in columns
                    ]
                )
                yield out.getvalue()
                out.seek(0)
                out.truncate(0)

    return stream()


def wishlist_demand(c, customer_id=None):
    # Explicit text grouping only; no guessed similarity between different requests.
    where, args = (" AND customer_id=?", [customer_id]) if customer_id else ("", [])
    return [
        dict(r)
        for r in c.execute(
            f"SELECT lower(trim(item_description)) AS request,COUNT(*) AS requests,COUNT(DISTINCT customer_id) AS customers FROM customer_wishlist WHERE fulfilled=0{where} GROUP BY lower(trim(item_description)) ORDER BY customers DESC,requests DESC,request LIMIT 20",
            args,
        )
    ]


def overview(db, q):
    bounds = period(q)
    where, args = date_where("b.bill_date", bounds)
    with db._conn() as c:
        totals = dict(
            c.execute(
                f"WITH sale AS ({BILLS}) SELECT COUNT(*) AS bill_count,COALESCE(SUM(total),0) AS revenue,COALESCE(SUM(balance),0) AS outstanding,COALESCE(SUM(pieces),0) AS pieces FROM sale b WHERE {where}",
                args,
            ).fetchone()
        )
        ew, ea = date_where("expense_date", bounds)
        totals["expenses"] = c.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM expenses WHERE {ew}", ea
        ).fetchone()[0]
        pw, pa = date_where("payment_date", bounds)
        totals["payments_collected"] = c.execute(
            f"SELECT COALESCE(SUM(amount),0) FROM payments WHERE {pw}", pa
        ).fetchone()[0]
        start = bounds[0]
        if not start:
            earliest = c.execute(
                f"SELECT MIN(b.bill_date) FROM bills b WHERE {where}", args
            ).fetchone()[0]
            start = date.fromisoformat(earliest[:10]) if earliest else bounds[1]
        daily = (bounds[1] - start).days <= 93
        group = "substr(b.bill_date,1,10)" if daily else "substr(b.bill_date,1,7)"
        found = {
            r["label"]: dict(r)
            for r in c.execute(
                f"SELECT {group} AS label,COUNT(*) AS bills,SUM(total) AS revenue FROM bills b WHERE {where} GROUP BY label ORDER BY label",
                args,
            )
        }
        series = []
        day = start
        while day <= bounds[1]:
            label = day.isoformat() if daily else day.strftime("%Y-%m")
            series.append(found.get(label, {"label": label, "bills": 0, "revenue": 0}))
            day = (
                day + timedelta(days=1)
                if daily
                else (day.replace(day=28) + timedelta(days=4)).replace(day=1)
            )
        base = f"FROM bill_items bi JOIN bills b ON b.id=bi.bill_id LEFT JOIN items i ON i.id=bi.item_id LEFT JOIN categories cat ON cat.id=i.category_id WHERE {where}"
        # Stored subtotal is discounted taxable value; avoid adding GST to sales rankings.
        item_sql = f"SELECT COALESCE(i.name,bi.item_name_snapshot) AS name,SUM(bi.quantity) AS quantity,SUM(bi.subtotal) AS revenue {base} GROUP BY COALESCE(bi.item_id,bi.item_name_snapshot)"
        qty = [
            dict(r)
            for r in c.execute(item_sql + " ORDER BY quantity DESC,name LIMIT 10", args)
        ]
        revenue = [
            dict(r)
            for r in c.execute(item_sql + " ORDER BY revenue DESC,name LIMIT 10", args)
        ]
        categories = [
            dict(r)
            for r in c.execute(
                f"SELECT COALESCE(bi.category_snapshot,cat.name,'Uncategorized') AS name,SUM(bi.quantity) AS quantity,SUM(bi.subtotal) AS revenue {base} GROUP BY COALESCE(bi.category_snapshot,cat.name,'Uncategorized') ORDER BY quantity DESC,name",
                args,
            )
        ]
        customers = [
            dict(r)
            for r in c.execute(
                f"SELECT c.id,c.name,c.phone,COUNT(*) AS visits,SUM(b.total) AS revenue FROM bills b JOIN customers c ON c.id=b.customer_id WHERE {where} GROUP BY c.id ORDER BY revenue DESC,c.id LIMIT 10",
                args,
            )
        ]
        return {
            "totals": totals,
            "series": series,
            "granularity": "day" if daily else "month",
            "top_quantity": qty,
            "top_revenue": revenue,
            "categories": categories,
            "customers": customers,
            "demand": wishlist_demand(c),
            "date_from": str(start),
            "date_to": str(bounds[1]),
        }
