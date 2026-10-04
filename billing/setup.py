"""Setup writes user-local environment configuration and never resets the database."""

import getpass
import os
import tempfile
from pathlib import Path
from .config import Settings, hash_password
from .payments import valid_upi_id


def save_settings(values, settings=None):
    settings = settings or Settings()
    path = settings.env_path
    existing = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k] = v
    if values.get("UPI_ID") and not valid_upi_id(values["UPI_ID"]):
        raise ValueError(
            "Enter a UPI ID such as yourshop@bank, not a phone or bank account number"
        )
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError("Configuration values must be single line")
        existing[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    temp = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(k + "=" + v for k, v in existing.items()) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def main():
    s = Settings()
    print("Shop Billing setup. Existing database and records will be preserved.")
    name = input(f"Company name [{s.company['name']}]: ").strip() or s.company["name"]
    password = getpass.getpass("Admin password (8+ characters; blank keeps existing): ")
    values = {"COMPANY_NAME": name}
    if password:
        if password != getpass.getpass("Confirm admin password: "):
            raise ValueError("Passwords do not match")
        values["ADMIN_PASSWORD_HASH"] = hash_password(password)
    elif not s.admin_hash:
        raise ValueError("An admin password is required for first setup")
    phone = input(
        "UPI-linked phone (optional reference only; blank keeps current): "
    ).strip()
    if phone:
        values["UPI_PHONE"] = phone
    print(
        "Copy the receiving UPI ID from your payment app. A phone number alone cannot identify the receiving account here."
    )
    upi = input(
        "Receiving UPI ID (e.g. yourshop@bank; optional, blank keeps current): "
    ).strip()
    if upi:
        values["UPI_ID"] = upi
    provider = (
        input("WhatsApp provider [disabled / meta] (blank keeps current): ").strip()
        or s.whatsapp_provider
    )
    if provider not in ("disabled", "meta"):
        raise ValueError("Supported providers: disabled, meta")
    values["WHATSAPP_PROVIDER"] = provider
    if provider == "meta":
        token = getpass.getpass("WhatsApp API access token (blank keeps current): ")
        if token:
            values["WHATSAPP_TOKEN"] = token
        for env, label in [
            ("WHATSAPP_PHONE_NUMBER_ID", "WhatsApp phone number ID"),
            ("WHATSAPP_BILL_TEMPLATE", "Approved invoice template name"),
            ("WHATSAPP_API_VERSION", "Meta API version"),
        ]:
            value = input(label + " (blank keeps current): ").strip()
            if value:
                values[env] = value
    if (
        input("Configure optional invoice/address/bank details now? [y/N]: ")
        .strip()
        .lower()
        == "y"
    ):
        for env, label in [
            ("COMPANY_ADDRESS", "Company address"),
            ("COMPANY_PHONE", "Company phone"),
            ("COMPANY_EMAIL", "Email"),
            ("COMPANY_GSTIN", "GSTIN"),
            ("BANK_NAME", "Bank name"),
            ("BANK_ACCOUNT", "Bank account"),
            ("BANK_IFSC", "IFSC"),
            ("BANK_BRANCH", "Bank branch"),
        ]:
            value = input(label + " (optional; blank keeps current): ").strip()
            if value:
                values[env] = value
    print("Configuration saved to", save_settings(values, s))
    print("Use startapp.bat for desktop or startserver.bat for the standalone server.")


if __name__ == "__main__":
    main()
