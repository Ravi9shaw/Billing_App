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
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


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
        load_env(Path(os.environ.get("BILLING_ENV_FILE", self.data_dir / ".env")))
        self.db_path = (
            Path(os.environ.get("BILLING_DB_PATH", self.data_dir / "cloth_shop.db"))
            .expanduser()
            .resolve()
        )
        self.port = int(os.environ.get("BILLING_PORT", "5000"))
        self.host = os.environ.get("BILLING_HOST", "0.0.0.0")
        self.server_url = os.environ.get("BILLING_SERVER_URL", "").rstrip("/")
        self.admin_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
        self.company = {
            key: os.environ.get(env, default)
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
                ("terms", "INVOICE_TERMS", "Thank you for your business."),
            ]
        }
        self.tax_mode = os.environ.get("DEFAULT_TAX_MODE", "exclusive")
        if self.tax_mode not in ("inclusive", "exclusive", "none"):
            raise ValueError("DEFAULT_TAX_MODE must be inclusive, exclusive or none")
        self.default_gst = os.environ.get("DEFAULT_GST_RATE", "5")
        self.whatsapp_provider = os.environ.get("WHATSAPP_PROVIDER", "disabled")
        self.whatsapp_token = os.environ.get("WHATSAPP_TOKEN", "")
        self.whatsapp_phone_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.whatsapp_version = os.environ.get("WHATSAPP_API_VERSION", "v23.0")
        self.whatsapp_base = os.environ.get(
            "WHATSAPP_API_BASE", "https://graph.facebook.com"
        ).rstrip("/")
        self.whatsapp_bill_template = os.environ.get("WHATSAPP_BILL_TEMPLATE", "")
        self.whatsapp_language = os.environ.get("WHATSAPP_LANGUAGE", "en")
        self.secure_cookie = (
            os.environ.get("BILLING_COOKIE_SECURE", "false").lower() == "true"
        )
        self.backup_keep = max(5, int(os.environ.get("BACKUP_KEEP", "30")))
        self.backup_seconds = max(
            5, int(os.environ.get("BACKUP_INTERVAL_SECONDS", "60"))
        )
        self.backup_dir = (
            Path(os.environ.get("BACKUP_DIR", self.data_dir / "backups"))
            .expanduser()
            .resolve()
        )
