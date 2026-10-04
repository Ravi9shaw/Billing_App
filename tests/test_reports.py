import csv
import io
from datetime import date

import pytest
from billing.reports import period
from .conftest import payload, key
from .test_api import client, login


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"period": "month", "month": "2024-02"}, ("2024-02-01", "2024-02-29")),
        ({"period": "month", "month": "2025-02"}, ("2025-02-01", "2025-02-28")),
        ({"period": "month", "month": "2026-10"}, ("2026-10-01", "2026-10-31")),
        ({"period": "lastmonth"}, ("2026-09-01", "2026-09-30")),
        ({"period": "3months"}, ("2026-07-01", "2026-09-30")),
        ({"period": "7days"}, ("2026-09-28", "2026-10-04")),
        ({"period": "year", "year": "2024"}, ("2024-01-01", "2024-12-31")),
    ],
)
def test_calendar_periods(query, expected):
    assert tuple(str(d) for d in period(query, date(2026, 10, 4))) == expected


def test_filtered_csv_contains_all_pages_and_only_selected_month(settings, db, item):
    c = client(settings)
    login(c)
    cid = db.catalog_write(
        "customers",
        {"name": "=Synthetic Ravi", "phone": "9000000000", "address": "Test address"},
    )
    for n in range(103):
        body = payload(db, item, customer_id=cid, paid_now=20, bill_date="2024-09-30")
        db.create_bill(body, "admin", settings, key())
    for day in ("2024-08-31", "2024-10-01"):
        db.create_bill(
            payload(db, item, customer_id=cid, paid_now=0, bill_date=day),
            "admin",
            settings,
            key(),
        )
    with db._conn(write=True) as conn:
        conn.execute("UPDATE bills SET bill_date='2024-09-30 23:59:59' WHERE id=1")
        conn.execute("UPDATE customers SET name='Changed name' WHERE id=?", (cid,))
    query = "period=month&month=2024-09&search=Synthetic"
    r = c.get("/api/reports/sales?" + query)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["summary"]["count"] == 103 and len(data["rows"]) == 50
    assert data["summary"]["outstanding"] == 103 * 85
    export = c.get("/api/reports/sales/csv?" + query + "&offset=100")
    rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert len(rows) == 103
    assert all(r["bill_date"].startswith("2024-09") for r in rows)
    assert rows[0]["customer_name"].startswith("'=")
    assert "2024-09-01-to-2024-09-30" in export.headers["content-disposition"]
    assert (
        c.get("/api/reports/sales?" + query + "&offset=100").json()["rows"].__len__()
        == 3
    )
    invalid = c.get(
        "/api/reports/sales?period=custom&date_from=2024-10-01&date_to=2024-09-01"
    )
    assert invalid.status_code == 400
    assert c.get("/api/reports/sales?period=month&month=bad").status_code == 400
    login(c, "employee")
    assert c.get("/api/reports/sales/csv").status_code == 403
    assert c.get("/api/reports/overview").status_code == 403


def test_balances_visits_expenses_and_customer_search(settings, db, item):
    c = client(settings)
    login(c)
    cid = db.catalog_write(
        "customers", {"name": "Ravi", "phone": "9000000000", "address": "Test address"}
    )
    for paid in (20, 30):
        db.create_bill(
            payload(db, item, customer_id=cid, paid_now=paid, bill_date="2024-09-15"),
            "admin",
            settings,
            key(),
        )
    data = c.get("/api/reports/balances?search=9000000000&outstanding=1").json()
    assert data["summary"]["count"] == 1 and data["overall"]["outstanding"] == 160
    assert data["rows"][0]["visits"] == 2
    profile = c.get(f"/api/customers/{cid}/profile").json()
    assert (
        profile["summary"]["visits"] == 2 and profile["summary"]["outstanding"] == 160
    )
    for day, category, amount in [
        ("2024-08-31", "Rent", 10),
        ("2024-09-01", "Rent", 20),
        ("2024-09-30", "Rent", 30),
        ("2024-09-15", "Travel", 40),
        ("2024-10-01", "Rent", 50),
    ]:
        c.post(
            "/api/expenses",
            json={"expense_date": day, "category": category, "amount": amount},
            headers={"Idempotency-Key": key()},
        ).raise_for_status()
    q = "period=month&month=2024-09&category=Rent"
    data = c.get("/api/reports/expenses?" + q).json()
    assert data["summary"] == {"count": 2, "total": 50}
    rows = list(
        csv.DictReader(
            io.StringIO(
                c.get("/api/reports/expenses/csv?" + q).content.decode("utf-8-sig")
            )
        )
    )
    assert len(rows) == 2 and sum(float(r["amount"]) for r in rows) == 50
    login(c, "employee")
    customer = c.get("/api/customers?search=Ravi").json()[0]
    assert customer["visits"] == 2 and customer["address"] == "Test address"
    assert "notes" not in customer
    assert c.get(f"/api/customers/{cid}/profile").status_code == 403


def test_overview_period_rankings_wishlist_and_category_snapshots(settings, db, item):
    c = client(settings)
    login(c)
    cid = db.catalog_write("customers", {"name": "Ravi"})
    other = db.catalog_write(
        "items", {"name": "Coat", "rate": 500, "category_id": 1, "gst_rate": 5}
    )
    body = payload(db, item, customer_id=cid, bill_date="2024-09-01", paid_now=0)
    body["items"][0]["quantity"] = 3
    body["items"][0]["discount"] = 20
    db.create_bill(body, "admin", settings, key())
    db.create_bill(
        payload(db, other, customer_id=cid, bill_date="2024-09-30", paid_now=0),
        "admin",
        settings,
        key(),
    )
    db.create_bill(
        payload(db, other, customer_id=cid, bill_date="2024-08-31"),
        "admin",
        settings,
        key(),
    )
    db.add_wishlist(cid, "Blue shirt")
    db.add_wishlist(cid, " blue shirt ")
    other_cid = db.catalog_write("customers", {"name": "Other"})
    db.add_wishlist(other_cid, "BLUE SHIRT")
    with db._conn(write=True) as conn:
        conn.execute("UPDATE categories SET name='Renamed' WHERE id=1")
    data = c.get("/api/reports/overview?period=month&month=2024-09").json()
    assert data["totals"]["bill_count"] == 2
    assert data["totals"]["revenue"] == 819
    assert len(data["series"]) == 30 and data["series"][1]["revenue"] == 0
    assert sum(r["revenue"] for r in data["series"]) == 819
    assert data["top_quantity"][0]["name"] == "Test shirt"
    assert data["top_revenue"][0]["name"] == "Coat"
    assert (
        data["categories"][0]["name"] == "General"
        and data["categories"][0]["quantity"] == 4
    )
    assert sum(r["revenue"] for r in data["categories"]) == 780
    assert data["customers"][0]["visits"] == 2
    assert data["demand"][0] == {"request": "blue shirt", "requests": 3, "customers": 2}
    assert c.get(f"/api/customers/{cid}/profile").json()["demand"][0]["requests"] == 2
    year = c.get("/api/reports/overview?period=year&year=2024").json()
    assert year["granularity"] == "month" and len(year["series"]) == 12
    assert year["totals"]["bill_count"] == 3


def test_lan_connection_is_authenticated_and_software_rendering_optional(
    settings, monkeypatch
):
    settings.public_url = "http://192.168.1.20:5000"
    c = client(settings)
    assert c.get("/api/connection").status_code == 401
    login(c, "employee")
    assert c.get("/api/connection").json()["url"] == settings.public_url
    assert c.get("/api/connection/qr").content.startswith(b"\x89PNG")
    from billing.desktop import configure_rendering

    for key_ in ("QT_QUICK_BACKEND", "QTWEBENGINE_CHROMIUM_FLAGS"):
        monkeypatch.delenv(key_, raising=False)
    monkeypatch.setenv("BILLING_SOFTWARE_RENDERING", "true")
    configure_rendering()
    import os

    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert "--disable-gpu" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--custom-override")
    configure_rendering()
    assert "--custom-override" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "--disable-features=Vulkan" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert os.environ["QSG_RHI_BACKEND"] == "opengl"
