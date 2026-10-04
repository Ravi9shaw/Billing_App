"""Persistent outbox. Provider acceptance is not proof of delivery.

Meta templates must exist and be approved in the configured WhatsApp account:
- bill: DOCUMENT header, one body parameter (invoice number)
- campaign: IMAGE header, one body parameter (offer/update text)
Ambiguous transport failures are never retried automatically.
"""

import json
import logging
import re
import httpx


def available(settings):
    if (
        settings.whatsapp_provider != "meta"
        or not settings.whatsapp_token
        or not settings.whatsapp_phone_id
    ):
        raise ValueError(
            "Configure Meta WhatsApp credentials in setup before sending. You can still download/share the PDF manually."
        )


def phone_number(value):
    value = re.sub(r"[\s()+-]", "", value or "")
    if not re.fullmatch(r"[1-9][0-9]{7,14}", value):
        raise ValueError("WhatsApp needs a phone number with country code")
    return value


def media_path(settings, identifier):
    if not re.fullmatch(r"[a-f0-9]{32}\.(png|jpg)", identifier or ""):
        raise ValueError("Select an uploaded PNG or JPG")
    path = settings.data_dir / "uploads" / identifier
    if not path.is_file():
        raise ValueError("Uploaded image not found")
    return path


def enqueue_bill(db, settings, bid, body, key):
    available(settings)
    if not settings.whatsapp_bill_template:
        raise ValueError("Configure an approved document invoice template first")
    with db._conn(write=True) as c:
        payload = {**body, "bill_id": bid}
        digest, old = db._idempotent(c, key, "whatsapp-bill", payload)
        if old:
            return old
        bill = c.execute("SELECT * FROM bills WHERE id=?", (bid,)).fetchone()
        if not bill:
            raise ValueError("Bill not found")
        customer = c.execute(
            "SELECT * FROM customers WHERE id=?", (bill["customer_id"],)
        ).fetchone()
        if not customer or not customer["whatsapp_opt_in"]:
            raise ValueError(
                "Customer must have recorded WhatsApp consent and a phone number"
            )
        phone = phone_number(customer["phone"])
        oid = c.execute(
            "INSERT INTO outbox(customer_id,phone,kind,payload) VALUES(?,?,'bill',?)",
            (
                customer["id"],
                phone,
                json.dumps(
                    {
                        "bill_id": bid,
                        "version": bill["version"],
                        "template": settings.whatsapp_bill_template,
                        "language": settings.whatsapp_language,
                    }
                ),
            ),
        ).lastrowid
        return db._result(c, key, "whatsapp-bill", digest, {"queued": 1, "id": oid})


def enqueue_campaign(db, settings, body, key):
    available(settings)
    template = str(body.get("template", "")).strip()
    text = str(body.get("text", "")).strip()
    if not re.fullmatch(r"[a-z0-9_]{1,100}", template) or not text or len(text) > 1000:
        raise ValueError("Use an approved template name and 1–1000 characters of text")
    media_path(settings, body.get("media"))
    with db._conn(write=True) as c:
        digest, old = db._idempotent(c, key, "campaign", body)
        if old:
            return old
        customers = c.execute(
            "SELECT id,phone FROM customers WHERE whatsapp_opt_in=1 AND phone IS NOT NULL"
        ).fetchall()
        recipients = []
        invalid = 0
        for customer in customers:
            try:
                recipients.append((customer["id"], phone_number(customer["phone"])))
            except ValueError:
                invalid += 1
        if not recipients:
            raise ValueError(
                "No opted-in customers have a valid international phone number"
            )
        payload = json.dumps(
            {
                "template": template,
                "text": text,
                "media": body["media"],
                "language": settings.whatsapp_language,
            }
        )
        c.executemany(
            "INSERT INTO outbox(customer_id,phone,kind,payload) VALUES(?,?,'campaign',?)",
            [(cid, phone, payload) for cid, phone in recipients],
        )
        db.audit(
            c,
            "admin",
            "queue-campaign",
            template,
            {"recipients": len(recipients), "invalid_phones_skipped": invalid},
        )
        return db._result(
            c,
            key,
            "campaign",
            digest,
            {"queued": len(recipients), "invalid_phones_skipped": invalid},
        )


class MetaProvider:
    def __init__(self, settings):
        self.settings = settings
        self.client = httpx.Client(
            timeout=httpx.Timeout(30, connect=10),
            headers={"Authorization": "Bearer " + settings.whatsapp_token},
        )
        self.base = f"{settings.whatsapp_base}/{settings.whatsapp_version}/{settings.whatsapp_phone_id}"

    def close(self):
        self.client.close()

    def upload(self, content, filename, mime):
        r = self.client.post(
            self.base + "/media",
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, content, mime)},
        )
        r.raise_for_status()
        return r.json()["id"]

    def send(self, phone, template, language, media_id, kind, text, filename=None):
        media = {"id": media_id}
        if kind == "document":
            media["filename"] = filename
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": language},
                "components": [
                    {"type": "header", "parameters": [{"type": kind, kind: media}]},
                    {"type": "body", "parameters": [{"type": "text", "text": text}]},
                ],
            },
        }
        r = self.client.post(self.base + "/messages", json=payload)
        r.raise_for_status()
        return r.json()["messages"][0]["id"]


def worker(db, settings, stop):
    while not stop.is_set():
        try:
            _run_worker(db, settings, stop)
            return
        except Exception:
            # A transient database/filesystem failure must not silently kill the queue.
            # Claimed sends stay 'sending' and are recovered as uncertain, never resent.
            logging.getLogger(__name__).error(
                "WhatsApp worker interrupted; retrying after 5 seconds"
            )
            if stop.wait(5):
                return


def _run_worker(db, settings, stop):
    if settings.whatsapp_provider == "disabled":
        return
    provider = MetaProvider(settings)
    try:
        while not stop.wait(0.5):
            with db._conn(write=True) as c:
                # An interrupted send might already have reached Meta; never resend it blindly.
                c.execute(
                    "UPDATE outbox SET status='uncertain',error='Interrupted send; check provider before resending' WHERE status='sending' AND updated_at < datetime('now','-5 minutes')"
                )
                row = c.execute(
                    "SELECT * FROM outbox WHERE status='queued' ORDER BY id LIMIT 1"
                ).fetchone()
                if not row:
                    continue
                customer = c.execute(
                    "SELECT whatsapp_opt_in,phone FROM customers WHERE id=?",
                    (row["customer_id"],),
                ).fetchone()
                try:
                    current_phone = (
                        phone_number(customer["phone"]) if customer else None
                    )
                except ValueError:
                    current_phone = None
                if (
                    not customer
                    or not customer["whatsapp_opt_in"]
                    or current_phone != row["phone"]
                ):
                    c.execute(
                        "UPDATE outbox SET status='skipped',error='Consent or phone changed' WHERE id=?",
                        (row["id"],),
                    )
                    continue
                c.execute(
                    "UPDATE outbox SET status='sending',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],),
                )
            try:
                available(settings)
                p = json.loads(row["payload"])
                if row["kind"] == "bill":
                    from .invoice import render_pdf

                    bill, lines = db.get_bill(p["bill_id"])
                    if not bill or bill["version"] != p["version"]:
                        raise ValueError(
                            "Bill changed after queuing; review and send the current invoice"
                        )
                    name = bill["bill_no"] + ".pdf"
                    media = provider.upload(
                        render_pdf(bill, lines, settings.company),
                        name,
                        "application/pdf",
                    )
                    mid = provider.send(
                        row["phone"],
                        p["template"],
                        p["language"],
                        media,
                        "document",
                        bill["bill_no"],
                        name,
                    )
                else:
                    path = media_path(settings, p["media"])
                    media = provider.upload(
                        path.read_bytes(),
                        path.name,
                        "image/png" if path.suffix == ".png" else "image/jpeg",
                    )
                    mid = provider.send(
                        row["phone"],
                        p["template"],
                        p["language"],
                        media,
                        "image",
                        p["text"],
                    )
                with db._conn(write=True) as c:
                    c.execute(
                        "UPDATE outbox SET status='accepted',provider_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (mid, row["id"]),
                    )
            except Exception as exc:
                # Keep provider messages/tokens out of UI and logs.
                status = (
                    "failed"
                    if isinstance(exc, ValueError)
                    or isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code < 500
                    else "uncertain"
                )
                explanation = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "Provider rejected request"
                    if status == "failed"
                    else "Delivery outcome unknown; check provider before resending"
                )
                with db._conn(write=True) as c:
                    c.execute(
                        "UPDATE outbox SET status=?,error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (status, explanation, row["id"]),
                    )
    finally:
        provider.close()
