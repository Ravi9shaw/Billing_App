"""Allowlisted setup editing. Database locations are deliberately read-only."""

import hashlib
import re
from urllib.parse import urlsplit

from .config import hash_password, verify_password
from .setup import save_settings
from .payments import valid_upi_id
from .database import Conflict

FIELDS = {
    "COMPANY_NAME": "name",
    "COMPANY_ADDRESS": "address",
    "COMPANY_PHONE": "phone",
    "COMPANY_EMAIL": "email",
    "COMPANY_GSTIN": "gstin",
    "BANK_NAME": "bank_name",
    "BANK_ACCOUNT": "bank_account",
    "BANK_IFSC": "bank_ifsc",
    "BANK_BRANCH": "bank_branch",
    "UPI_ID": "upi_id",
    "UPI_PHONE": "upi_phone",
    "INVOICE_TERMS": "terms",
}
ATTRS = {
    "DEFAULT_TAX_MODE": "tax_mode",
    "DEFAULT_GST_RATE": "default_gst",
    "WHATSAPP_PROVIDER": "whatsapp_provider",
    "WHATSAPP_PHONE_NUMBER_ID": "whatsapp_phone_id",
    "WHATSAPP_API_VERSION": "whatsapp_version",
    "WHATSAPP_BILL_TEMPLATE": "whatsapp_bill_template",
    "WHATSAPP_LANGUAGE": "whatsapp_language",
    "BILLING_HOST": "host",
    "BILLING_PORT": "port",
    "BILLING_PUBLIC_URL": "public_url",
    "BACKUP_KEEP": "backup_keep",
    "BACKUP_INTERVAL_SECONDS": "backup_seconds",
}


def revision(settings):
    content = settings.env_path.read_bytes() if settings.env_path.exists() else b""
    return hashlib.sha256(content).hexdigest()


def public_settings(settings):
    values = {k: settings.company[v] for k, v in FIELDS.items()}
    values.update({k: str(getattr(settings, v)) for k, v in ATTRS.items()})
    return {
        "values": values,
        "revision": revision(settings),
        "locked": sorted(
            settings.environment_overrides
            & (set(FIELDS) | set(ATTRS) | {"WHATSAPP_TOKEN", "ADMIN_PASSWORD_HASH"})
        ),
        "whatsapp_token_set": bool(settings.whatsapp_token),
        "config_file": str(settings.env_path),
        "database": str(settings.db_path),
    }


def update(settings, body):
    if body.get("revision") != revision(settings):
        raise Conflict("Configuration changed. Reload it before saving.")
    values = body.get("values")
    if not isinstance(values, dict) or set(values) - (set(FIELDS) | set(ATTRS)):
        raise ValueError("Unknown configuration field")
    current = public_settings(settings)["values"]
    changed = {}
    for key, value in values.items():
        if (
            not isinstance(value, str)
            or len(value) > 2000
            or "\n" in value
            or "\r" in value
        ):
            raise ValueError(
                "Configuration values must be single-line text, at most 2000 characters"
            )
        value = value.strip()
        if value != current[key]:
            if key in settings.environment_overrides:
                raise ValueError(
                    f"{key} is controlled by the operating-system environment"
                )
            changed[key] = value
    effective = {**current, **changed}
    if not effective["COMPANY_NAME"]:
        raise ValueError("Company name is required")
    if effective["UPI_ID"] and not valid_upi_id(effective["UPI_ID"]):
        raise ValueError("Enter the receiving UPI ID from your payment app")
    if effective["DEFAULT_TAX_MODE"] not in ("inclusive", "exclusive", "none"):
        raise ValueError("Invalid GST mode")
    from .money import decimal

    if decimal(effective["DEFAULT_GST_RATE"]) > 100:
        raise ValueError("GST rate must be between 0 and 100")
    for name, low, high in [
        ("BILLING_PORT", 1024, 65535),
        ("BACKUP_KEEP", 5, 1000),
        ("BACKUP_INTERVAL_SECONDS", 5, 86400),
    ]:
        if not low <= int(effective[name]) <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
    if effective["BILLING_HOST"] not in ("0.0.0.0", "127.0.0.1"):
        raise ValueError("Choose Wi-Fi access or this computer only")
    if effective["BILLING_PUBLIC_URL"]:
        url = urlsplit(effective["BILLING_PUBLIC_URL"])
        if (
            url.scheme not in ("http", "https")
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Use an HTTP(S) public URL without credentials, query or fragment"
            )
    if effective["WHATSAPP_PROVIDER"] not in ("disabled", "meta"):
        raise ValueError("Choose disabled or Meta")
    token = body.get("whatsapp_token", "")
    if (
        not isinstance(token, str)
        or len(token) > 4000
        or "\n" in token
        or "\r" in token
    ):
        raise ValueError("Invalid WhatsApp token")
    if token or body.get("clear_whatsapp_token"):
        if "WHATSAPP_TOKEN" in settings.environment_overrides:
            raise ValueError("WhatsApp token is controlled by the environment")
        changed["WHATSAPP_TOKEN"] = (
            "" if body.get("clear_whatsapp_token") else token.strip()
        )
    if effective["WHATSAPP_PROVIDER"] == "meta":
        if not (
            changed.get("WHATSAPP_TOKEN", settings.whatsapp_token)
            and effective["WHATSAPP_PHONE_NUMBER_ID"]
        ):
            raise ValueError(
                "Meta requires an access token and phone-number ID; otherwise choose disabled"
            )
        if not re.fullmatch(r"v\d+\.\d+", effective["WHATSAPP_API_VERSION"]):
            raise ValueError("Use a Graph API version such as v23.0")
    password = body.get("new_password", "")
    if password:
        if "ADMIN_PASSWORD_HASH" in settings.environment_overrides:
            raise ValueError("Admin password is controlled by the environment")
        if not verify_password(body.get("current_password", ""), settings.admin_hash):
            raise PermissionError("Current admin password is incorrect")
        if password != body.get("confirm_password"):
            raise ValueError("New passwords do not match")
        changed["ADMIN_PASSWORD_HASH"] = hash_password(password)
    # The running host owns its current port until graceful shutdown.
    # A host-only change reuses that port after shutdown.
    if int(effective["BILLING_PORT"]) != settings.port:
        import socket

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((effective["BILLING_HOST"], int(effective["BILLING_PORT"])))
        except OSError as exc:
            raise ValueError(
                "The selected host/port is unavailable. Choose a free port before saving."
            ) from exc
    save_settings(changed, settings)
    return {
        "password_changed": bool(password),
        "port": int(effective["BILLING_PORT"]),
        "public_url": effective["BILLING_PUBLIC_URL"],
        "changed": sorted(changed),
    }
