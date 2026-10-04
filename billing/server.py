"""Authenticated API. Business writes always go through the shared Database."""

import hashlib
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from .config import Settings, verify_password
from .database import Database, Conflict
from .money import money, iso_date
from .models import BillBody, ItemBody, CustomerBody, PaymentBody, ConfigurationBody
from .version import API_VERSION, BUILD_ID, CAPABILITIES, source_build

FRONTEND = Path(__file__).resolve().parent / "web"


def create_app(settings=None):
    settings = settings or Settings()
    db = Database(settings.db_path, settings.backup_dir)
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        from .whatsapp import worker

        threads = [
            threading.Thread(
                target=db.backup_loop,
                args=(stop, settings.backup_seconds, settings.backup_keep),
                daemon=True,
            ),
            threading.Thread(target=worker, args=(db, settings, stop), daemon=True),
        ]
        for t in threads:
            t.start()
        yield
        stop.set()
        for t in threads:
            t.join(timeout=35)
        if db.dirty.is_set():
            try:
                db.backup_database(reason="shutdown")
            except Exception:
                logging.exception("Shutdown backup failed")

    app = FastAPI(
        title="Shop Billing", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.db = db
    app.state.settings = settings
    app.state.instance_id = secrets.token_hex(16)
    app.state.restart_callback = None
    app.state.restarting = threading.Event()
    config_lock = threading.Lock()
    attempts = {}
    attempt_lock = threading.Lock()

    @app.middleware("http")
    async def security(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if app.state.restarting.is_set():
                return JSONResponse(
                    {
                        "detail": "Server is restarting. Reconnect and retry the same request."
                    },
                    status_code=503,
                )
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    {"detail": "Cross-origin write denied"}, status_code=403
                )
            if request.headers.get("x-billing-request") != "1":
                return JSONResponse(
                    {"detail": "Missing request header"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = (
            "SAMEORIGIN" if request.url.path.endswith("/invoice") else "DENY"
        )
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api") else "no-cache"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_shape(req, e):
        messages = [
            ".".join(str(x) for x in error["loc"][1:]) + ": " + error["msg"]
            for error in e.errors()
        ]
        return JSONResponse({"detail": "; ".join(messages)}, status_code=422)

    @app.exception_handler(Conflict)
    async def conflict(req, e):
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(ValueError)
    async def bad_value(req, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def forbidden(req, e):
        return JSONResponse({"detail": str(e)}, status_code=403)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity(req, e):
        return JSONResponse(
            {
                "detail": "This record conflicts with existing data. Check duplicate phone/barcode/name and linked records."
            },
            status_code=409,
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def unavailable(req, e):
        logging.exception("Database operation failed")
        return JSONResponse(
            {
                "detail": "Database is busy or unavailable. Retry with the same request; do not create a second bill."
            },
            status_code=503,
        )

    def session_digest(token):
        return hashlib.sha256((token + settings.admin_hash).encode()).hexdigest()

    def session(request: Request):
        token = request.cookies.get("billing_session", "")
        digest = session_digest(token)
        with db._conn() as c:
            r = c.execute(
                "SELECT role FROM sessions WHERE token_hash=? AND expires>?",
                (digest, time.time()),
            ).fetchone()
        if not r:
            raise HTTPException(401, "Choose employee or sign in as admin")
        return r["role"]

    def admin(role=Depends(session)):
        if role != "admin":
            raise HTTPException(403, "Admin access required")
        return role

    def key(request):
        return request.headers.get("idempotency-key", "")

    def listing(rs):
        return [dict(r) for r in rs]

    @app.get("/api/health")
    def health():
        with db._conn() as c:
            c.execute("SELECT 1").fetchone()
        return {
            "status": "ok",
            "application": "cloth-shop-billing",
            "api_version": API_VERSION,
            "build": BUILD_ID,
            "instance_id": app.state.instance_id,
            "capabilities": CAPABILITIES,
            "stale_files": source_build() != BUILD_ID,
            "storage_id": hashlib.sha256(str(settings.db_path).encode()).hexdigest(),
            "restarting": app.state.restarting.is_set(),
        }

    @app.get("/api/config")
    def config():
        return {
            "company": settings.company,
            "tax_mode": settings.tax_mode,
            "gst_rate": settings.default_gst,
            "configured": bool(settings.admin_hash),
            "whatsapp": settings.whatsapp_provider != "disabled",
        }

    class Login(BaseModel):
        role: str
        password: str = Field(default="", max_length=1000)

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        if body.role not in ("employee", "admin"):
            raise ValueError("Choose employee or admin")
        if not settings.admin_hash:
            raise HTTPException(
                503, "Run setupapp.bat to configure the admin password first"
            )
        if body.role == "admin":
            address = request.client.host
            with attempt_lock:
                recent = [x for x in attempts.get(address, []) if x > time.time() - 300]
                if len(recent) >= 8:
                    raise HTTPException(
                        429, "Too many attempts. Try again in five minutes."
                    )
                recent.append(time.time())
                attempts[address] = recent
            if not verify_password(body.password, settings.admin_hash):
                raise HTTPException(403, "Incorrect admin password")
        token = secrets.token_urlsafe(32)
        with db._conn(write=True) as c:
            old = request.cookies.get("billing_session", "")
            c.execute(
                "DELETE FROM sessions WHERE token_hash=? OR expires<?",
                (session_digest(old), time.time()),
            )
            c.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (
                    session_digest(token),
                    body.role,
                    time.time() + 8 * 3600,
                ),
            )
        response.set_cookie(
            "billing_session",
            token,
            httponly=True,
            samesite="strict",
            secure=settings.secure_cookie,
            max_age=8 * 3600,
        )
        return {"role": body.role}

    @app.get("/api/session")
    def who(role=Depends(session)):
        return {"role": role}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        with db._conn(write=True) as c:
            c.execute(
                "DELETE FROM sessions WHERE token_hash=?",
                (session_digest(request.cookies.get("billing_session", "")),),
            )
        response.delete_cookie("billing_session")
        return {"ok": True}

    @app.get("/api/admin/configuration")
    def configuration(role=Depends(admin)):
        from .admin_config import public_settings

        result = public_settings(settings)
        result["restart_supported"] = app.state.restart_callback is not None
        result["build"] = BUILD_ID
        return result

    def schedule_restart(payload):
        from starlette.background import BackgroundTask

        if app.state.restart_callback is None:
            raise HTTPException(
                409,
                "This server was started by an external host. Restart that host, or use python -m billing --run-server for managed restart.",
            )
        app.state.restarting.set()
        return JSONResponse(
            {**payload, "previous_instance": app.state.instance_id},
            background=BackgroundTask(app.state.restart_callback),
        )

    @app.post("/api/admin/configuration")
    def configuration_save(body: ConfigurationBody, role=Depends(admin)):
        from .admin_config import update

        with config_lock:
            if app.state.restart_callback is None:
                raise HTTPException(
                    409,
                    "Start the backend with python -m billing --run-server to save and restart from Configuration.",
                )
            db.backup_database(reason="before-configuration")
            result = update(settings, body.model_dump())
            with db._conn(write=True) as c:
                db.audit(
                    c,
                    role,
                    "configuration-update",
                    "settings",
                    {"changed_fields": result["changed"]},
                )
            return schedule_restart({**result, "restart_requested": True})

    @app.post("/api/server/restart")
    def restart_server(role=Depends(admin)):
        with config_lock:
            db.backup_database(reason="before-restart")
            return schedule_restart(
                {
                    "restart_requested": True,
                    "port": settings.port,
                    "public_url": settings.public_url,
                }
            )

    @app.get("/api/reports/{kind}/csv")
    def report_csv(kind: str, request: Request, role=Depends(admin)):
        from .reports import export_csv, query
        from fastapi.responses import StreamingResponse

        _, _, _, _, bounds = query(kind, request.query_params)
        filename = f"{kind}-{bounds[0] or 'all'}-to-{bounds[1]}.csv"
        return StreamingResponse(
            export_csv(db, kind, request.query_params),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/reports/{kind}")
    def reporting(kind: str, request: Request, role=Depends(admin)):
        from .reports import report, overview

        return (
            overview(db, request.query_params)
            if kind == "overview"
            else report(db, kind, request.query_params)
        )

    def connection_info():
        from .launcher import lan_ip

        url = (
            settings.public_url
            or f"http://{lan_ip() if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}"
        )
        return {
            "url": url,
            "local_only": settings.host in ("localhost", "127.0.0.1", "::1"),
            "status": "running",
        }

    @app.get("/api/connection")
    def connection(role=Depends(session)):
        return connection_info()

    @app.get("/api/connection/qr")
    def connection_qr(role=Depends(session)):
        import qrcode
        from io import BytesIO

        out = BytesIO()
        qrcode.make(connection_info()["url"]).save(out, format="PNG")
        return Response(out.getvalue(), media_type="image/png")

    @app.get("/api/customers/{cid}/profile")
    def customer_profile(cid: int, role=Depends(admin)):
        from .reports import BILLS, wishlist_demand

        with db._conn() as c:
            row = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
            if not row:
                raise HTTPException(404, "Customer not found")
            summary = dict(
                c.execute(
                    f"WITH sale AS ({BILLS}) SELECT COUNT(*) AS visits,MAX(bill_date) AS last_visit,COALESCE(SUM(total),0) AS total,COALESCE(SUM(paid_amount),0) AS paid,COALESCE(SUM(balance),0) AS outstanding FROM sale WHERE customer_id=?",
                    (cid,),
                ).fetchone()
            )
            return {
                "customer": dict(row),
                "summary": summary,
                "demand": wishlist_demand(c, cid),
            }

    @app.get("/api/categories")
    def categories(role=Depends(session)):
        return listing(db.get_categories())

    @app.get("/api/subtypes")
    def subtypes(category_id: int, role=Depends(session)):
        return listing(db.get_subtypes(category_id))

    @app.post("/api/categories")
    def category_write(body: dict, role=Depends(admin)):
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValueError("Name required")
        db.add_category(name)
        return {"ok": True}

    @app.post("/api/subtypes")
    def subtype_write(body: dict, role=Depends(admin)):
        if not str(body.get("name", "")).strip():
            raise ValueError("Name required")
        db.add_subtype(body["category_id"], body["name"].strip())
        return {"ok": True}

    @app.get("/api/items")
    def items(
        search: str = "",
        category_id: int | None = None,
        offset: int = 0,
        limit: int = 100,
        role=Depends(session),
    ):
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)
        with db._conn() as c:
            return listing(
                c.execute(
                    """SELECT i.*,cat.name AS category_name FROM items i JOIN categories cat ON cat.id=i.category_id WHERE i.active=1 AND (? IS NULL OR i.category_id=?) AND (i.name LIKE ? OR i.barcode=?) ORDER BY i.name LIMIT ? OFFSET ?""",
                    (category_id, category_id, f"%{search}%", search, limit, offset),
                ).fetchall()
            )

    @app.get("/api/items/{iid}")
    def item(iid: int, role=Depends(session)):
        row = db.get_item_by_id(iid)
        if not row or not row["active"]:
            raise HTTPException(404, "Item no longer active")
        return dict(row)

    @app.put("/api/categories/{cid}")
    def category_rename(cid: int, body: dict, role=Depends(admin)):
        name = str(body.get("name", "")).strip()
        if not name:
            raise ValueError("Name required")
        db.rename_category(cid, name)
        return {"ok": True}

    @app.delete("/api/categories/{cid}")
    def category_delete(cid: int, role=Depends(admin)):
        db.delete_category(cid)
        return {"ok": True}

    @app.post("/api/items")
    def add_item(body: ItemBody, request: Request, role=Depends(admin)):
        body = body.model_dump(exclude_unset=True, mode="json")
        return {"id": db.catalog_write("items", body, request_key=key(request))}

    @app.put("/api/items/{iid}")
    def edit_item(iid: int, body: ItemBody, request: Request, role=Depends(admin)):
        body = body.model_dump(exclude_unset=True, mode="json")
        return {"id": db.catalog_write("items", body, iid, request_key=key(request))}

    @app.delete("/api/items/{iid}")
    def delete_item(iid: int, body: dict, role=Depends(admin)):
        with db._conn(write=True) as c:
            r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if not r or r["version"] != body.get("version"):
                raise Conflict("Item changed. Reload before deleting")
            db.audit(c, role, "archive-item", iid, dict(r))
            c.execute("UPDATE items SET active=0,version=version+1 WHERE id=?", (iid,))
        return {"ok": True}

    @app.get("/api/customers")
    def customers(search: str = "", offset: int = 0, role=Depends(session)):
        with db._conn() as c:
            rs = c.execute(
                "SELECT customers.*,(SELECT COUNT(*) FROM bills WHERE customer_id=customers.id) AS visits,(SELECT MAX(bill_date) FROM bills WHERE customer_id=customers.id) AS last_visit FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY name,id LIMIT 100 OFFSET ?",
                (f"%{search}%", f"%{search}%", max(offset, 0)),
            ).fetchall()
        if role == "employee":
            return [
                {
                    k: r[k]
                    for k in ["id", "name", "phone", "address", "visits", "last_visit"]
                }
                for r in rs
            ]
        return listing(rs)

    @app.post("/api/customers")
    def add_customer(body: CustomerBody, request: Request, role=Depends(admin)):
        body = body.model_dump(exclude_unset=True, mode="json")
        return {"id": db.catalog_write("customers", body, request_key=key(request))}

    @app.put("/api/customers/{cid}")
    def edit_customer(
        cid: int, body: CustomerBody, request: Request, role=Depends(admin)
    ):
        body = body.model_dump(exclude_unset=True, mode="json")
        return {
            "id": db.catalog_write("customers", body, cid, request_key=key(request))
        }

    @app.delete("/api/customers/{cid}")
    def delete_customer(cid: int, body: dict, role=Depends(admin)):
        with db._conn(write=True) as c:
            r = c.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
            if not r or r["version"] != body.get("version"):
                raise Conflict("Customer changed")
            db.audit(c, role, "delete-customer", cid, dict(r))
            c.execute("DELETE FROM customers WHERE id=?", (cid,))
        return {"ok": True}

    @app.get("/api/customers/{cid}/history")
    def history(cid: int, role=Depends(admin)):
        return listing(db.get_customer_purchase_history(cid))

    @app.get("/api/customers/{cid}/wishlist")
    def wishlist(cid: int, role=Depends(admin)):
        return listing(db.get_wishlist(cid))

    @app.post("/api/customers/{cid}/wishlist")
    def add_wish(cid: int, body: dict, role=Depends(admin)):
        db.add_wishlist(cid, body["description"])
        return {"ok": True}

    @app.put("/api/wishlist/{wid}")
    def fulfill(wid: int, body: dict, role=Depends(admin)):
        db.set_wishlist_fulfilled(wid, int(bool(body.get("fulfilled"))))
        return {"ok": True}

    @app.post("/api/bills/quote")
    def quote(body: BillBody, role=Depends(session)):
        body = body.model_dump(exclude_unset=True, mode="json")
        with db._conn() as c:
            return db.prepare(c, body, role, settings)

    @app.post("/api/bills")
    def bill_create(body: BillBody, request: Request, role=Depends(session)):
        body = body.model_dump(exclude_unset=True, mode="json")
        return db.create_bill(body, role, settings, key(request))

    @app.put("/api/bills/{bid}")
    def bill_edit(bid: int, body: BillBody, request: Request, role=Depends(admin)):
        body = body.model_dump(exclude_unset=True, mode="json")
        return db.create_bill(body, role, settings, key(request), edit_id=bid)

    @app.get("/api/bills")
    def bills(
        search: str = "",
        date_from: str | None = None,
        date_to: str | None = None,
        offset: int = 0,
        role=Depends(admin),
    ):
        with db._conn() as c:
            return listing(
                c.execute(
                    """SELECT b.*,COALESCE(json_extract(b.customer_snapshot,'$.name'),c.name,'Walk-in') AS customer_name,COALESCE((SELECT SUM(amount) FROM payments WHERE bill_id=b.id),0) AS paid_amount FROM bills b LEFT JOIN customers c ON c.id=b.customer_id WHERE (b.bill_no LIKE ? OR c.name LIKE ?) AND (? IS NULL OR b.bill_date>=?) AND (? IS NULL OR b.bill_date<?) ORDER BY b.id DESC LIMIT 100 OFFSET ?""",
                    (
                        f"%{search}%",
                        f"%{search}%",
                        date_from,
                        date_from,
                        date_to,
                        date_to + "T99" if date_to else None,
                        max(offset, 0),
                    ),
                ).fetchall()
            )

    @app.get("/api/bills/{bid}")
    def bill_get(bid: int, role=Depends(session)):
        bill, items = db.get_bill(bid)
        if not bill:
            raise HTTPException(404, "Bill not found")
        return {"bill": bill, "items": items}

    @app.delete("/api/bills/{bid}")
    def bill_void(bid: int, body: dict, role=Depends(admin)):
        db.void_bill(bid, body.get("version"), str(body.get("reason", "")))
        return {"ok": True}

    @app.post("/api/bills/{bid}/payments")
    def payment(bid: int, body: PaymentBody, request: Request, role=Depends(admin)):
        body = body.model_dump(exclude_unset=True, mode="json")
        return db.receive_payment(bid, body, key(request))

    @app.get("/api/bills/{bid}/payments")
    def payments(bid: int, role=Depends(admin)):
        return listing(db.get_bill_payment_history(bid))

    @app.get("/api/bills/{bid}/invoice")
    def invoice(bid: int, role=Depends(session)):
        from .invoice import render_html

        b, lines = db.get_bill(bid)
        if not b:
            raise HTTPException(404, "Bill not found")
        return HTMLResponse(render_html(b, lines, settings.company))

    @app.get("/api/bills/{bid}/pdf")
    def pdf(bid: int, role=Depends(session)):
        from .invoice import render_pdf

        b, lines = db.get_bill(bid)
        if not b:
            raise HTTPException(404, "Bill not found")
        return Response(
            render_pdf(b, lines, settings.company),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{b["bill_no"]}.pdf"'
            },
        )

    @app.get("/api/balances")
    def balances(role=Depends(admin)):
        return db.get_customer_balances()

    @app.get("/api/expenses")
    def expenses(
        date_from: str | None = None, date_to: str | None = None, role=Depends(admin)
    ):
        return listing(db.get_expenses(date_from, date_to))

    @app.post("/api/expenses")
    def expense(body: dict, request: Request, role=Depends(admin)):
        amount = money(body.get("amount", 0))
        if amount <= 0 or not str(body.get("category", "")).strip():
            raise ValueError("Category and positive amount required")
        with db._conn(write=True) as c:
            digest, old = db._idempotent(c, key(request), "expense", body)
            if old:
                return old
            eid = c.execute(
                "INSERT INTO expenses(expense_date,category,amount,payment_mode,description,created_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    iso_date(body.get("expense_date")),
                    body["category"].strip(),
                    float(amount),
                    body.get("payment_mode", "Cash"),
                    body.get("description"),
                ),
            ).lastrowid
            db.audit(c, role, "expense", eid, body)
            return db._result(c, key(request), "expense", digest, {"id": eid})

    @app.delete("/api/expenses/{eid}")
    def delete_expense(eid: int, role=Depends(admin)):
        with db._conn(write=True) as c:
            row = c.execute("SELECT * FROM expenses WHERE id=?", (eid,)).fetchone()
            if row:
                db.audit(c, role, "delete-expense", eid, dict(row))
                c.execute("DELETE FROM expenses WHERE id=?", (eid,))
        return {"ok": True}

    @app.get("/api/stats")
    def stats(
        date_from: str | None = None, date_to: str | None = None, role=Depends(admin)
    ):
        return {
            "totals": db.stat_totals(date_from, date_to),
            "monthly": listing(db.stat_monthly_sales()),
            "top_items": listing(db.stat_top_items(date_from, date_to)),
        }

    @app.get("/api/reconciliation")
    def reconciliation(role=Depends(admin)):
        with db._conn() as c:
            return listing(
                c.execute(
                    """SELECT b.id,b.bill_no,b.version FROM bills b WHERE EXISTS(SELECT 1 FROM bill_items i WHERE i.bill_id=b.id AND i.stock_deducted IS NULL) OR EXISTS(SELECT 1 FROM payments p WHERE p.bill_id=b.id AND p.notes='Migrated from historical bill') ORDER BY b.id LIMIT 100"""
                ).fetchall()
            )

    @app.post("/api/bills/{bid}/reconcile")
    def reconcile(bid: int, body: dict, role=Depends(admin)):
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ValueError("Record the evidence/reason for this correction")
        with db._conn(write=True) as c:
            b = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
            if not b or b["version"] != body.get("version"):
                raise Conflict("Bill changed; reload before reconciling")
            before = db._bill_snapshot(c, bid)
            for payment_id in body.get("remove_migration_payments", []):
                payment = c.execute(
                    "SELECT * FROM payments WHERE id=? AND bill_id=? AND notes='Migrated from historical bill'",
                    (payment_id, bid),
                ).fetchone()
                if not payment:
                    raise ValueError(
                        "Only the flagged historical migration payments can be corrected here"
                    )
                c.execute("DELETE FROM payments WHERE id=?", (payment_id,))
            for line_id, deduction in body.get("stock_deductions", {}).items():
                line = c.execute(
                    "SELECT * FROM bill_items WHERE id=? AND bill_id=?", (line_id, bid)
                ).fetchone()
                if (
                    not line
                    or line["stock_deducted"] is not None
                    or not isinstance(deduction, int)
                    or isinstance(deduction, bool)
                    or not 0 <= deduction <= line["quantity"]
                ):
                    raise ValueError(
                        "Enter an actual deducted quantity between zero and the sold quantity for an unverified line"
                    )
                c.execute(
                    "UPDATE bill_items SET stock_deducted=? WHERE id=?",
                    (deduction, line_id),
                )
            c.execute("UPDATE bills SET version=version+1 WHERE id=?", (bid,))
            db.audit(
                c,
                role,
                "reconcile-bill",
                bid,
                {"before": before, "correction": body, "reason": reason},
            )
        return {"ok": True}

    @app.get("/api/status")
    def status(role=Depends(admin)):
        with db._conn() as c:
            ambiguous = c.execute(
                "SELECT COUNT(*) FROM payments WHERE notes='Migrated from historical bill'"
            ).fetchone()[0]
            unknown = c.execute(
                "SELECT COUNT(*) FROM bill_items WHERE stock_deducted IS NULL"
            ).fetchone()[0]
        return {
            "database": db.path,
            "backup_error": db.backup_error,
            "backups": [
                p.name for p in sorted(db.backup_dir.glob("*.db"), reverse=True)[:10]
            ],
            "legacy_payments_to_review": ambiguous,
            "legacy_stock_lines_to_review": unknown,
        }

    @app.post("/api/backup")
    def backup(role=Depends(admin)):
        return {"file": Path(db.backup_database(reason="manual")).name}

    @app.get("/api/audit")
    def audit(offset: int = 0, role=Depends(admin)):
        with db._conn() as c:
            return listing(
                c.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT 100 OFFSET ?",
                    (max(offset, 0),),
                ).fetchall()
            )

    @app.get("/api/export")
    def export(role=Depends(admin)):
        import io
        import csv
        import zipfile

        buffer = io.BytesIO()
        with db._conn() as c, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            for table in [
                "categories",
                "subtypes",
                "items",
                "customers",
                "bills",
                "bill_items",
                "payments",
                "expenses",
                "customer_wishlist",
                "audit_log",
            ]:
                cur = c.execute(f"SELECT * FROM {table}")
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow([d[0] for d in cur.description])
                for row in cur:
                    writer.writerow(
                        [
                            (
                                "'" + v
                                if isinstance(v, str)
                                and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                                else v
                            )
                            for v in row
                        ]
                    )
                z.writestr(table + ".csv", out.getvalue())
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="billing-export.zip"'
            },
        )

    @app.post("/api/bills/{bid}/whatsapp")
    def send_bill(bid: int, body: dict, request: Request, role=Depends(session)):
        from .whatsapp import enqueue_bill

        return enqueue_bill(db, settings, bid, body, key(request))

    @app.post("/api/media")
    async def upload(request: Request, role=Depends(admin)):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 5 * 1024 * 1024:
                raise HTTPException(413, "Image must be at most 5 MB")
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            extension = "png"
        elif content.startswith(b"\xff\xd8\xff"):
            extension = "jpg"
        else:
            raise ValueError("Upload a PNG or JPG image")
        from PIL import Image
        from io import BytesIO

        try:
            with Image.open(BytesIO(content)) as img:
                if img.width * img.height > 25000000:
                    raise ValueError("Image dimensions are too large")
                img.verify()
        except Exception:
            raise ValueError("Invalid image")
        folder = settings.data_dir / "uploads"
        folder.mkdir(exist_ok=True)
        identifier = secrets.token_hex(16) + "." + extension
        (folder / identifier).write_bytes(content)
        return {"id": identifier}

    @app.get("/api/campaigns/preview")
    def campaign_preview(role=Depends(admin)):
        from .whatsapp import phone_number

        with db._conn() as c:
            rows = c.execute(
                "SELECT phone FROM customers WHERE whatsapp_opt_in=1"
            ).fetchall()
        count = 0
        for row in rows:
            try:
                phone_number(row["phone"])
                count += 1
            except ValueError:
                pass
        return {"recipients": count}

    @app.post("/api/campaigns")
    def campaign(body: dict, request: Request, role=Depends(admin)):
        from .whatsapp import enqueue_campaign

        return enqueue_campaign(db, settings, body, key(request))

    @app.get("/api/messages")
    def messages(role=Depends(admin)):
        with db._conn() as c:
            return listing(
                c.execute(
                    "SELECT id,phone,kind,status,error,provider_id,created_at FROM outbox ORDER BY id DESC LIMIT 100"
                ).fetchall()
            )

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")

    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
    return app
