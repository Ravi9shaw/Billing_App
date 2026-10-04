"""One SQLite writer boundary for desktop and web. Never use on a network share."""

import hashlib
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .queries import Queries, SCHEMA
from .money import calculate, money, decimal, iso_date


class Conflict(ValueError):
    pass


class Database(Queries):
    def __init__(self, path, backup_dir=None):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir = Path(backup_dir or Path(self.path).parent / "backups")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.dirty = threading.Event()
        self.backup_error = None
        with sqlite3.connect(self.path, timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")
            needs_migration = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='app_migrations'"
                ).fetchone()
                is None
            )
            has_data = (
                c.execute("SELECT 1 FROM sqlite_master WHERE name='bills'").fetchone()
                is not None
            )
        if needs_migration and has_data:
            self.backup_database(reason="before-migration")
        self._migrate()

    @contextmanager
    def _conn(self, write=False):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        c.execute("PRAGMA busy_timeout=30000")
        try:
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield c
            changed = c.total_changes > 0
            c.commit()
            if changed:
                self.dirty.set()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def _migrate(self):
        with self._conn(write=True) as c:
            if c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='app_migrations'"
            ).fetchone():
                return
            legacy_payments = bool(
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='payments'"
                ).fetchone()
            )
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    c.execute(statement)
            additions = {
                "items": {
                    "gst_rate": "REAL NOT NULL DEFAULT 5",
                    "hsn": "TEXT",
                    "version": "INTEGER NOT NULL DEFAULT 1",
                },
                "customers": {
                    "version": "INTEGER NOT NULL DEFAULT 1",
                    "whatsapp_opt_in": "INTEGER NOT NULL DEFAULT 0",
                    "consent_at": "TEXT",
                    "gstin": "TEXT",
                    "pin_code": "TEXT",
                },
                "bills": {
                    "taxable_amount": "REAL",
                    "gst_rate": "REAL NOT NULL DEFAULT 0",
                    "gst_amount": "REAL NOT NULL DEFAULT 0",
                    "tax_mode": "TEXT NOT NULL DEFAULT 'legacy'",
                    "interstate": "INTEGER NOT NULL DEFAULT 0",
                    "cgst": "REAL NOT NULL DEFAULT 0",
                    "sgst": "REAL NOT NULL DEFAULT 0",
                    "igst": "REAL NOT NULL DEFAULT 0",
                    "version": "INTEGER NOT NULL DEFAULT 1",
                    "customer_snapshot": "TEXT",
                    "company_snapshot": "TEXT",
                    "metadata": "TEXT NOT NULL DEFAULT '{}'",
                },
                "bill_items": {
                    "gst_rate": "REAL NOT NULL DEFAULT 0",
                    "gst_amount": "REAL NOT NULL DEFAULT 0",
                    "discount": "REAL NOT NULL DEFAULT 0",
                    "stock_deducted": "INTEGER",
                    "hsn": "TEXT",
                    "cgst": "REAL NOT NULL DEFAULT 0",
                    "sgst": "REAL NOT NULL DEFAULT 0",
                    "igst": "REAL NOT NULL DEFAULT 0",
                },
            }
            for table, cols in additions.items():
                present = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for col, definition in cols.items():
                    if col not in present:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            statements = [
                "CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,payment_date TEXT NOT NULL,amount REAL NOT NULL CHECK(amount>0),payment_mode TEXT NOT NULL DEFAULT 'Cash',notes TEXT)",
                "CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,expense_date TEXT NOT NULL,category TEXT NOT NULL,amount REAL NOT NULL CHECK(amount>0),payment_mode TEXT NOT NULL DEFAULT 'Cash',description TEXT,created_at TEXT)",
                "CREATE TABLE app_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)",
                "CREATE TABLE invoice_sequences(date TEXT PRIMARY KEY,sequence INTEGER NOT NULL)",
                "CREATE TABLE request_keys(key TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_hash TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
                "CREATE TABLE audit_log(id INTEGER PRIMARY KEY,at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,actor TEXT NOT NULL,action TEXT NOT NULL,entity TEXT NOT NULL,snapshot TEXT NOT NULL)",
                "CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,role TEXT NOT NULL,expires REAL NOT NULL)",
                "CREATE TABLE outbox(id INTEGER PRIMARY KEY,customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,phone TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',provider_id TEXT,error TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
                "CREATE INDEX IF NOT EXISTS idx_payments_bill ON payments(bill_id)",
                "CREATE INDEX IF NOT EXISTS idx_payments_customer ON payments(customer_id)",
                "CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date)",
                "CREATE INDEX IF NOT EXISTS idx_bills_customer ON bills(customer_id)",
                "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status,id)",
            ]
            for statement in statements:
                c.execute(statement)
            if not self._column_exists(c, "expenses", "created_at"):
                c.execute("ALTER TABLE expenses ADD COLUMN created_at TEXT")
            # Only the pre-payments schema can be classified as fully paid legacy sales.
            if not legacy_payments:
                c.execute(
                    "INSERT INTO payments(bill_id,customer_id,payment_date,amount,payment_mode,notes) SELECT id,customer_id,date(bill_date),total,COALESCE(payment_mode,'Cash'),'One-time legacy schema migration' FROM bills WHERE total>0"
                )
            # Preserve unknown payment/stock history. Do not fabricate retrospective values.
            for r in c.execute("SELECT bill_no FROM bills").fetchall():
                parts = r["bill_no"].split("-")
                if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                    key = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:8]}"
                    c.execute(
                        "INSERT INTO invoice_sequences VALUES(?,?) ON CONFLICT(date) DO UPDATE SET sequence=MAX(sequence,excluded.sequence)",
                        (key, int(parts[2])),
                    )
            if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
                c.execute("INSERT INTO categories(name) VALUES('General')")
            # Capture currently stored customer details before later profile edits/deletion.
            for bill in c.execute(
                "SELECT id,customer_id FROM bills WHERE customer_snapshot IS NULL"
            ).fetchall():
                customer = c.execute(
                    "SELECT * FROM customers WHERE id=?", (bill["customer_id"],)
                ).fetchone()
                c.execute(
                    "UPDATE bills SET customer_snapshot=? WHERE id=?",
                    (
                        json.dumps(
                            dict(customer) if customer else {"name": "Walk-in customer"}
                        ),
                        bill["id"],
                    ),
                )
            c.execute("INSERT INTO app_migrations VALUES(1,CURRENT_TIMESTAMP)")
            self.audit(
                c,
                "system",
                "migration",
                "database",
                {
                    "legacy_payment_table": legacy_payments,
                    "historical_values_preserved": True,
                },
            )

    def audit(self, c, actor, action, entity, snapshot):
        c.execute(
            "INSERT INTO audit_log(actor,action,entity,snapshot) VALUES(?,?,?,?)",
            (actor, action, str(entity), json.dumps(snapshot, default=str)),
        )

    def backup_database(self, dest_folder=None, reason="automatic"):
        dest = Path(dest_folder or self.backup_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / f"cloth_shop_{datetime.now():%Y%m%d_%H%M%S_%f}_{reason}.db"
        temporary = target.with_suffix(".partial")
        try:
            with (
                sqlite3.connect(self.path, timeout=30) as src,
                sqlite3.connect(temporary) as dst,
            ):
                src.backup(dst)
                if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
            os.replace(temporary, target)
            self.backup_error = None
            return str(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def backup_loop(self, stop, seconds, keep):
        while not stop.wait(seconds):
            if self.dirty.is_set():
                self.dirty.clear()
                try:
                    self.backup_database()
                    files = sorted(self.backup_dir.glob("*_automatic.db"), reverse=True)
                    for f in files[keep:]:
                        f.unlink()
                except Exception as e:
                    self.dirty.set()
                    self.backup_error = type(e).__name__
                    logging.exception("Automatic backup failed")

    def _next_bill_no_conn(self, c, bill_date=None):
        day = iso_date(bill_date)
        c.execute(
            "INSERT INTO invoice_sequences VALUES(?,1) ON CONFLICT(date) DO UPDATE SET sequence=sequence+1",
            (day,),
        )
        seq = c.execute(
            "SELECT sequence FROM invoice_sequences WHERE date=?", (day,)
        ).fetchone()[0]
        return f"INV-{day.replace('-', '')}-{seq:04d}"

    def _idempotent(self, c, key, kind, payload):
        if not isinstance(key, str) or not 16 <= len(key) <= 100:
            raise ValueError("A request key is required")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        old = c.execute("SELECT * FROM request_keys WHERE key=?", (key,)).fetchone()
        if old:
            if old["kind"] != kind or old["payload_hash"] != digest:
                raise Conflict("Request key was already used with different data")
            return digest, json.loads(old["result"])
        return digest, None

    def _result(self, c, key, kind, digest, result):
        c.execute(
            "INSERT INTO request_keys(key,kind,payload_hash,result) VALUES(?,?,?,?)",
            (key, kind, digest, json.dumps(result)),
        )
        return result

    def prepare(self, c, payload, role, settings):
        lines = []
        mode = payload.get("tax_mode") or settings.tax_mode
        interstate = bool(payload.get("interstate", False))
        if role != "admin" and (mode != settings.tax_mode or interstate):
            raise PermissionError("Only admin can change tax settings")
        for line in payload.get("items", []):
            item = c.execute(
                "SELECT * FROM items WHERE id=? AND active=1", (line.get("item_id"),)
            ).fetchone()
            if not item:
                raise ValueError("Select an active catalog item")
            if line.get("version") != item["version"]:
                raise Conflict(
                    f"{item['name']} changed. Refresh the catalog and check the bill."
                )
            rate = line.get("rate") if line.get("rate") is not None else item["rate"]
            discount = line.get("discount", 0)
            gst = (
                line.get("gst_rate")
                if line.get("gst_rate") is not None
                else item["gst_rate"]
            )
            if role != "admin" and (
                money(rate) != money(item["rate"])
                or money(discount) != 0
                or decimal(gst) != decimal(item["gst_rate"])
            ):
                raise PermissionError("Only admin can change rates, discounts or GST")
            lines.append(
                dict(
                    item_id=item["id"],
                    name=item["name"],
                    quantity=line.get("quantity", 1),
                    rate=rate,
                    discount=discount,
                    gst_rate=gst,
                    hsn=item["hsn"],
                )
            )
        return calculate(lines, mode, interstate)

    def create_bill(self, payload, role, settings, key, edit_id=None):
        with self._conn(write=True) as c:
            kind = f"edit:{edit_id}" if edit_id else "bill"
            digest, old = self._idempotent(c, key, kind, payload)
            if old:
                return old
            totals = self.prepare(c, payload, role, settings)
            day = iso_date(payload.get("bill_date"))
            if role != "admin" and day != iso_date():
                raise PermissionError("Only admin can change the bill date")
            customer_id = payload.get("customer_id")
            customer = (
                c.execute(
                    "SELECT * FROM customers WHERE id=?", (customer_id,)
                ).fetchone()
                if customer_id
                else None
            )
            if customer_id and not customer:
                raise Conflict("Customer no longer exists")
            new_details = payload.get("customer", {})
            if (
                not customer
                and not new_details.get("name", "").strip()
                and any(new_details.get(k) for k in ("phone", "address", "notes"))
            ):
                raise ValueError(
                    "Enter a customer name or leave customer details blank for a walk-in sale"
                )
            if not customer and payload.get("customer", {}).get("name", "").strip():
                new = payload["customer"]
                phone = (new.get("phone") or "").strip() or None
                customer = (
                    c.execute(
                        "SELECT * FROM customers WHERE phone=?", (phone,)
                    ).fetchone()
                    if phone
                    else None
                )
                if not customer:
                    cid = c.execute(
                        "INSERT INTO customers(name,phone,address,notes) VALUES(?,?,?,?)",
                        (
                            new["name"].strip(),
                            phone,
                            new.get("address") or None,
                            new.get("notes") or None,
                        ),
                    ).lastrowid
                    customer = c.execute(
                        "SELECT * FROM customers WHERE id=?", (cid,)
                    ).fetchone()
                customer_id = customer["id"]
            metadata = {
                k: str(payload.get("metadata", {}).get(k, "")).strip()[:200]
                for k in [
                    "book_no",
                    "eway_no",
                    "eway_date",
                    "po_no",
                    "po_date",
                    "transport",
                    "lr_no",
                    "party_gstin",
                    "pin_code",
                ]
            }
            if customer:
                metadata["party_gstin"] = (
                    metadata["party_gstin"] or customer["gstin"] or ""
                )
                metadata["pin_code"] = (
                    metadata["pin_code"] or customer["pin_code"] or ""
                )
            snapshot = dict(customer) if customer else {"name": "Walk-in customer"}
            company_snapshot = settings.company
            payment_mode = payload.get("payment_mode", "Cash")
            if payment_mode not in ["Cash", "Card", "UPI", "Other"]:
                raise ValueError("Unknown payment mode")
            if edit_id:
                if role != "admin":
                    raise PermissionError("Admin required")
                bill = c.execute(
                    "SELECT * FROM bills WHERE id=?", (edit_id,)
                ).fetchone()
                if not bill or bill["version"] != payload.get("version"):
                    raise Conflict("Bill changed. Reload it before editing")
                paid = money(
                    c.execute(
                        "SELECT COALESCE(SUM(amount),0) FROM payments WHERE bill_id=?",
                        (edit_id,),
                    ).fetchone()[0]
                )
                if paid > money(totals["total"]):
                    raise ValueError(
                        "New total is below collected payments; reconcile payments before editing"
                    )
                if bill["customer_id"] == customer_id and bill["customer_snapshot"]:
                    snapshot = json.loads(bill["customer_snapshot"])
                if bill["company_snapshot"]:
                    company_snapshot = json.loads(bill["company_snapshot"])
                self._restore_stock(c, edit_id)
                self.audit(
                    c,
                    role,
                    "edit-bill-before",
                    edit_id,
                    self._bill_snapshot(c, edit_id),
                )
                c.execute("DELETE FROM bill_items WHERE bill_id=?", (edit_id,))
                bid = edit_id
                bill_no = bill["bill_no"]
                c.execute("UPDATE bills SET version=version+1 WHERE id=?", (bid,))
                c.execute(
                    "UPDATE payments SET customer_id=? WHERE bill_id=?",
                    (customer_id, bid),
                )
            else:
                paid = money(
                    payload["paid_now"]
                    if payload.get("paid_now") is not None
                    else totals["total"]
                )
                if paid > money(totals["total"]):
                    raise ValueError("Paid amount exceeds total")
                bill_no = self._next_bill_no_conn(c, day)
                bid = c.execute(
                    "INSERT INTO bills(bill_no,subtotal,total) VALUES(?,0,0)",
                    (bill_no,),
                ).lastrowid
            c.execute(
                """UPDATE bills SET customer_id=?,bill_date=?,subtotal=?,discount_amount=?,discount_percent=?,total=?,payment_mode=?,taxable_amount=?,gst_amount=?,gst_rate=?,tax_mode=?,interstate=?,cgst=?,sgst=?,igst=?,customer_snapshot=?,company_snapshot=?,metadata=? WHERE id=?""",
                (
                    customer_id,
                    day,
                    totals["subtotal"],
                    totals["discount_amount"],
                    0,
                    totals["total"],
                    payment_mode,
                    totals["taxable_amount"],
                    totals["gst_amount"],
                    0,
                    totals["tax_mode"],
                    int(totals["interstate"]),
                    totals["cgst"],
                    totals["sgst"],
                    totals["igst"],
                    json.dumps(snapshot),
                    json.dumps(company_snapshot),
                    json.dumps(metadata),
                    bid,
                ),
            )
            for line in totals["items"]:
                stock = c.execute(
                    "SELECT stock_qty FROM items WHERE id=?", (line["item_id"],)
                ).fetchone()[0]
                deducted = min(max(stock, 0), line["quantity"])
                c.execute(
                    "UPDATE items SET stock_qty=stock_qty-? WHERE id=?",
                    (deducted, line["item_id"]),
                )
                c.execute(
                    """INSERT INTO bill_items(bill_id,item_id,item_name_snapshot,quantity,rate,subtotal,gst_rate,gst_amount,discount,stock_deducted,hsn,cgst,sgst,igst) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        bid,
                        line["item_id"],
                        line["name"],
                        line["quantity"],
                        line["rate"],
                        line["subtotal"],
                        line["gst_rate"],
                        line["gst_amount"],
                        line["discount"],
                        deducted,
                        line["hsn"],
                        line["cgst"],
                        line["sgst"],
                        line["igst"],
                    ),
                )
            if not edit_id and paid > 0:
                c.execute(
                    "INSERT INTO payments(bill_id,customer_id,payment_date,amount,payment_mode,notes) VALUES(?,?,?,?,?,?)",
                    (
                        bid,
                        customer_id,
                        day,
                        float(paid),
                        payment_mode,
                        "Initial payment",
                    ),
                )
            self.audit(
                c,
                role,
                "edit-bill" if edit_id else "create-bill",
                bid,
                {"total": totals["total"], "request_key": key},
            )
            return self._result(
                c, key, kind, digest, {"bill_id": bid, "bill_no": bill_no}
            )

    def _bill_snapshot(self, c, bid):
        b = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
        return {
            "bill": dict(b) if b else None,
            "items": [
                dict(x)
                for x in c.execute("SELECT * FROM bill_items WHERE bill_id=?", (bid,))
            ],
            "payments": [
                dict(x)
                for x in c.execute("SELECT * FROM payments WHERE bill_id=?", (bid,))
            ],
        }

    def _restore_stock(self, c, bid):
        for line in c.execute("SELECT * FROM bill_items WHERE bill_id=?", (bid,)):
            if line["item_id"] and line["stock_deducted"] is None:
                raise Conflict(
                    "Historical stock deduction is unknown. Reconcile this bill before voiding or editing; stock was not changed."
                )
            if line["item_id"]:
                c.execute(
                    "UPDATE items SET stock_qty=stock_qty+? WHERE id=?",
                    (line["stock_deducted"], line["item_id"]),
                )

    def void_bill(self, bid, version, reason):
        if not reason.strip():
            raise ValueError("A reason is required")
        with self._conn(write=True) as c:
            b = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
            if not b or b["version"] != version:
                raise Conflict("Bill changed or is already voided")
            self._restore_stock(c, bid)
            self.audit(
                c,
                "admin",
                "void-bill",
                bid,
                {**self._bill_snapshot(c, bid), "reason": reason},
            )
            c.execute("DELETE FROM bills WHERE id=?", (bid,))

    def get_bill(self, bid):
        with self._conn() as c:
            b = c.execute(
                """SELECT b.*,c.name AS customer_name,c.phone AS customer_phone,c.address AS customer_address,COALESCE((SELECT SUM(amount) FROM payments WHERE bill_id=b.id),0) AS paid_amount FROM bills b LEFT JOIN customers c ON c.id=b.customer_id WHERE b.id=?""",
                (bid,),
            ).fetchone()
            if not b:
                return None, []
            b = dict(b)
            b["balance"] = float(max(money(b["total"]) - money(b["paid_amount"]), 0))
            if b["customer_snapshot"]:
                snapshot = json.loads(b["customer_snapshot"])
                b.update(
                    customer_name=snapshot.get("name"),
                    customer_phone=snapshot.get("phone"),
                    customer_address=snapshot.get("address"),
                )
            b["metadata"] = json.loads(b["metadata"] or "{}")
            b["company"] = (
                json.loads(b["company_snapshot"]) if b["company_snapshot"] else None
            )
            return b, [
                dict(x)
                for x in c.execute("SELECT * FROM bill_items WHERE bill_id=?", (bid,))
            ]

    def receive_payment(self, bid, payload, key):
        amount = money(payload["amount"])
        if amount <= 0:
            raise ValueError("Amount must be positive")
        with self._conn(write=True) as c:
            digest, old = self._idempotent(c, key, f"payment:{bid}", payload)
            if old:
                return old
            bill = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
            if not bill:
                raise ValueError("Bill not found")
            paid = money(
                c.execute(
                    "SELECT COALESCE(SUM(amount),0) FROM payments WHERE bill_id=?",
                    (bid,),
                ).fetchone()[0]
            )
            if amount > money(bill["total"]) - paid:
                raise Conflict("Payment exceeds the current balance. Reload the bill.")
            pid = c.execute(
                "INSERT INTO payments(bill_id,customer_id,payment_date,amount,payment_mode,notes) VALUES(?,?,?,?,?,?)",
                (
                    bid,
                    bill["customer_id"],
                    iso_date(payload.get("payment_date")),
                    float(amount),
                    payload.get("payment_mode", "Cash"),
                    payload.get("notes", ""),
                ),
            ).lastrowid
            c.execute("UPDATE bills SET version=version+1 WHERE id=?", (bid,))
            self.audit(
                c, "admin", "payment", bid, {"payment_id": pid, "amount": float(amount)}
            )
            return self._result(c, key, f"payment:{bid}", digest, {"id": pid})

    def catalog_write(self, table, data, record_id=None, request_key=None):
        allowed = {
            "items": [
                "name",
                "category_id",
                "subtype_id",
                "barcode",
                "size",
                "color",
                "rate",
                "stock_qty",
                "gst_rate",
                "hsn",
                "active",
            ],
            "customers": [
                "name",
                "phone",
                "address",
                "notes",
                "gstin",
                "pin_code",
                "whatsapp_opt_in",
            ],
        }
        if table not in allowed:
            raise ValueError("Unknown record")
        values = {k: data[k] for k in allowed[table] if k in data}
        if not str(values.get("name", "")).strip():
            raise ValueError("Name is required")
        values["name"] = values["name"].strip()
        for field in [
            "phone",
            "barcode",
            "address",
            "notes",
            "size",
            "color",
            "hsn",
            "gstin",
            "pin_code",
        ]:
            if field in values:
                values[field] = (values[field] or "").strip() or None
        for field in ["rate", "gst_rate"]:
            if field in values:
                values[field] = float(money(values[field]))
        if values.get("gst_rate", 0) > 100:
            raise ValueError("GST rate cannot exceed 100")
        if table == "items":
            if (
                not isinstance(values.get("stock_qty", 0), int)
                or values.get("stock_qty", 0) < 0
            ):
                raise ValueError("Stock must be a nonnegative whole number")
            values.setdefault("gst_rate", 5)
        with self._conn(write=True) as c:
            operation = f"catalog:{table}:{record_id or 'new'}"
            if request_key:
                digest, old = self._idempotent(c, request_key, operation, data)
                if old:
                    return old["id"]
            if table == "items" and values.get("subtype_id"):
                sub = c.execute(
                    "SELECT category_id FROM subtypes WHERE id=?",
                    (values["subtype_id"],),
                ).fetchone()
                if not sub or sub[0] != values["category_id"]:
                    raise ValueError("Brand does not belong to category")
            if record_id:
                old = c.execute(
                    f"SELECT * FROM {table} WHERE id=?", (record_id,)
                ).fetchone()
                if not old or old["version"] != data.get("version"):
                    raise Conflict("Record changed. Reload before saving")
                self.audit(c, "admin", "edit-before", f"{table}:{record_id}", dict(old))
                if (
                    table == "items"
                    and values.get("stock_qty", old["stock_qty"]) != old["stock_qty"]
                ):
                    # Stock version changes on billing too, so stale stock edits cannot overwrite sales.
                    if data.get("expected_stock") != old["stock_qty"]:
                        raise Conflict(
                            "Stock changed during this edit. Reload before saving"
                        )
                c.execute(
                    f"UPDATE {table} SET "
                    + ",".join(f"{k}=?" for k in values)
                    + ",version=version+1 WHERE id=?",
                    (*values.values(), record_id),
                )
            else:
                record_id = c.execute(
                    f"INSERT INTO {table}("
                    + ",".join(values)
                    + ") VALUES("
                    + ",".join("?" for _ in values)
                    + ")",
                    tuple(values.values()),
                ).lastrowid
            if table == "customers" and "whatsapp_opt_in" in values:
                c.execute(
                    "UPDATE customers SET consent_at=? WHERE id=?",
                    (
                        datetime.now().isoformat()
                        if values["whatsapp_opt_in"]
                        else None,
                        record_id,
                    ),
                )
            self.audit(c, "admin", "save", f"{table}:{record_id}", values)
            if request_key:
                self._result(c, request_key, operation, digest, {"id": record_id})
            return record_id
