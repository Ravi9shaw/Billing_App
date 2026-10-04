from fastapi.testclient import TestClient
from billing.server import create_app
from .conftest import key


def client(settings):
    c = TestClient(create_app(settings))
    c.headers["X-Billing-Request"] = "1"
    return c


def login(c, role="admin"):
    r = c.post("/api/login", json={"role": role, "password": "test-password-123"})
    assert r.status_code == 200


def test_permissions_and_authoritative_amounts(settings):
    c = client(settings)
    assert c.get("/api/health").json()["application"] == "cloth-shop-billing"
    assert c.get("/api/items").status_code == 401
    login(c)
    iid = c.post(
        "/api/items",
        json={
            "name": "Item",
            "category_id": 1,
            "rate": 100,
            "gst_rate": 5,
            "stock_qty": 5,
        },
    ).json()["id"]
    login(c, "employee")
    for method, url, body in [
        ("post", "/api/items", {}),
        ("get", "/api/customers/1/history", None),
        ("get", "/api/audit", None),
        ("get", "/api/export", None),
        ("post", "/api/backup", {}),
        ("post", "/api/campaigns", {}),
    ]:
        assert (
            getattr(c, method)(
                url, **({"json": body} if body is not None else {})
            ).status_code
            == 403
        )
    body = {
        "items": [{"item_id": iid, "version": 1, "quantity": 1, "rate": 1}],
        "paid_now": 0,
    }
    assert (
        c.post("/api/bills", json=body, headers={"Idempotency-Key": key()}).status_code
        == 403
    )
    body["items"][0]["rate"] = 100
    body["total"] = 1
    k = key()
    r = c.post("/api/bills", json=body, headers={"Idempotency-Key": k})
    assert r.status_code == 200, r.text
    bid = r.json()["bill_id"]
    assert c.get("/api/bills/" + str(bid)).json()["bill"]["total"] == 105
    assert (
        c.post("/api/bills", json=body, headers={"Idempotency-Key": k}).json()
        == r.json()
    )
    assert (
        c.request(
            "DELETE", "/api/bills/" + str(bid), json={"version": 1, "reason": "test"}
        ).status_code
        == 403
    )
    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/items").status_code == 401


def test_optional_api_and_errors(settings):
    c = client(settings)
    login(c)
    for name in ["Alice", "Bob"]:
        assert (
            c.post(
                "/api/customers", json={"name": name, "phone": None, "address": None}
            ).status_code
            == 200
        )
    r = c.post(
        "/api/items",
        json={"name": "Item", "category_id": 1, "barcode": "A", "rate": 10},
    )
    assert r.status_code == 200
    assert (
        c.post(
            "/api/items",
            json={"name": "Item2", "category_id": 1, "barcode": "A", "rate": 10},
        ).status_code
        == 409
    )
    assert (
        c.post(
            "/api/items", json={"name": "Bad", "category_id": 1, "rate": -1}
        ).status_code
        == 422
    )
    iid = r.json()["id"]
    for mode, expected in [("inclusive", 10), ("exclusive", 10.5), ("none", 10)]:
        r = c.post(
            "/api/bills",
            json={
                "tax_mode": mode,
                "items": [{"item_id": iid, "version": 1, "quantity": 1}],
            },
            headers={"Idempotency-Key": key()},
        )
        assert r.status_code == 200, r.text
        b = c.get("/api/bills/" + str(r.json()["bill_id"])).json()["bill"]
        assert b["total"] == expected
        assert c.get("/api/bills/" + str(b["id"]) + "/pdf").content.startswith(b"%PDF")
    assert (
        c.post("/api/login", json={"role": "admin", "password": "wrong"}).status_code
        == 403
    )
    assert (
        c.post(
            "/api/login",
            json={"role": "employee"},
            headers={"Origin": "https://other.test"},
        ).status_code
        == 403
    )


def test_expenses_profiles_reconciliation_and_invoice_frame(settings):
    c = client(settings)
    login(c)
    eid = c.post(
        "/api/expenses",
        json={"category": "Rent", "amount": 10},
        headers={"Idempotency-Key": key()},
    ).json()["id"]
    assert c.get("/api/expenses").json()[0]["created_at"]
    assert c.delete("/api/expenses/" + str(eid)).status_code == 200
    cid = c.post(
        "/api/customers",
        json={
            "name": "Existing",
            "phone": "919000000001",
            "address": "Keep address",
            "notes": "Keep notes",
        },
    ).json()["id"]
    iid = c.post(
        "/api/items", json={"name": "Item", "category_id": 1, "rate": 100}
    ).json()["id"]
    login(c, "employee")
    response = c.post(
        "/api/bills",
        json={
            "items": [{"item_id": iid, "version": 1, "quantity": 1}],
            "customer": {"name": "Existing", "phone": "919000000001"},
            "paid_now": 0,
        },
        headers={"Idempotency-Key": key()},
    )
    bid = response.json()["bill_id"]
    assert (
        c.get("/api/bills/" + str(bid) + "/invoice").headers["x-frame-options"]
        == "SAMEORIGIN"
    )
    login(c)
    row = next(x for x in c.get("/api/customers").json() if x["id"] == cid)
    assert row["address"] == "Keep address" and row["notes"] == "Keep notes"
    with c.app.state.db._conn(write=True) as conn:
        pid = conn.execute(
            "INSERT INTO payments(bill_id,customer_id,payment_date,amount,payment_mode,notes) VALUES(?,?,'2020-01-01',105,'Cash','Migrated from historical bill')",
            (bid, cid),
        ).lastrowid
    body = {
        "version": 1,
        "reason": "Synthetic verified correction",
        "remove_migration_payments": [pid],
    }
    assert c.post("/api/bills/" + str(bid) + "/reconcile", json=body).status_code == 200
    assert c.get("/api/bills/" + str(bid)).json()["bill"]["balance"] == 105
    assert c.post("/api/bills/" + str(bid) + "/reconcile", json=body).status_code == 409
    assert c.get("/api/export").content.startswith(b"PK")
