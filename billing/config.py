"""Environment configuration; secrets never enter frontend configuration."""

import hashlib
import hmac
import os
import secrets
from pathlib import Path
from dataclasses import dataclass, field


def hash_password(password):
    if len(password) < 8:
        raise ValueError("Use an admin password of at least 8 characters.")
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt.encode(), n=16384, r=8, p=1
    ).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password, encoded):
    try:
        kind, salt, digest = encoded.split("$")
        return kind == "scrypt" and hmac.compare_digest(
            hashlib.scrypt(
                password.encode(), salt=salt.encode(), n=16384, r=8, p=1
            ).hex(),
            digest,
        )
    except (ValueError, TypeError):
        return False


def load_env(path):
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


@dataclass
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("BILLING_DATA_DIR", Path.home() / ".cloth_shop_billing")
        )
        .expanduser()
        .resolve()
    )

    def __post_init__(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.env_path = Path(os.environ.get("BILLING_ENV_FILE", self.data_dir / ".env"))
        self.environment_overrides = frozenset(os.environ)
        self.values = {**load_env(self.env_path), **os.environ}
        values = self.values
        self.db_path = (
            Path(values.get("BILLING_DB_PATH", self.data_dir / "cloth_shop.db"))
            .expanduser()
            .resolve()
        )
        self.port = int(values.get("BILLING_PORT", "5000"))
        self.host = values.get("BILLING_HOST", "0.0.0.0")
        self.public_url = values.get("BILLING_PUBLIC_URL", "").rstrip("/")
        if self.public_url:
            from urllib.parse import urlsplit

            url = urlsplit(self.public_url)
            if (
                url.scheme not in ("http", "https")
                or not url.netloc
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "BILLING_PUBLIC_URL must be an HTTP(S) shop URL without credentials, query or fragment"
                )
        self.server_url = values.get("BILLING_SERVER_URL", "").rstrip("/")
        self.admin_hash = values.get("ADMIN_PASSWORD_HASH", "")
        self.company = {
            key: values.get(env, default)
            for key, env, default in [
                ("name", "COMPANY_NAME", "Your Company"),
                ("address", "COMPANY_ADDRESS", ""),
                ("phone", "COMPANY_PHONE", ""),
                ("email", "COMPANY_EMAIL", ""),
                ("gstin", "COMPANY_GSTIN", ""),
                ("bank_name", "BANK_NAME", ""),
                ("bank_account", "BANK_ACCOUNT", ""),
                ("bank_ifsc", "BANK_IFSC", ""),
                ("bank_branch", "BANK_BRANCH", ""),
                ("upi_id", "UPI_ID", ""),
                ("upi_phone", "UPI_PHONE", ""),
                ("terms", "INVOICE_TERMS", "Thank you for your business."),
            ]
        }
        self.tax_mode = values.get("DEFAULT_TAX_MODE", "exclusive")
        if self.tax_mode not in ("inclusive", "exclusive", "none"):
            raise ValueError("DEFAULT_TAX_MODE must be inclusive, exclusive or none")
        self.default_gst = values.get("DEFAULT_GST_RATE", "5")
        self.whatsapp_provider = values.get("WHATSAPP_PROVIDER", "disabled")
        self.whatsapp_token = values.get("WHATSAPP_TOKEN", "")
        self.whatsapp_phone_id = values.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.whatsapp_version = values.get("WHATSAPP_API_VERSION", "v23.0")
        self.whatsapp_base = values.get(
            "WHATSAPP_API_BASE", "https://graph.facebook.com"
        ).rstrip("/")
        self.whatsapp_bill_template = values.get("WHATSAPP_BILL_TEMPLATE", "")
        self.whatsapp_language = values.get("WHATSAPP_LANGUAGE", "en")
        self.secure_cookie = (
            values.get("BILLING_COOKIE_SECURE", "false").lower() == "true"
        )
        self.backup_keep = max(5, int(values.get("BACKUP_KEEP", "30")))
        self.backup_seconds = max(5, int(values.get("BACKUP_INTERVAL_SECONDS", "60")))
        self.backup_dir = (
            Path(values.get("BACKUP_DIR", self.data_dir / "backups"))
            .expanduser()
            .resolve()
        )
