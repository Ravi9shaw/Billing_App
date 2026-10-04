import uuid
import pytest
from billing.config import Settings, hash_password
from billing.database import Database


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("test-password-123"))
    monkeypatch.setenv("COMPANY_NAME", "Synthetic Shop")
    monkeypatch.setenv("WHATSAPP_PROVIDER", "disabled")
    return Settings()


@pytest.fixture
def db(settings):
    return Database(settings.db_path)


@pytest.fixture
def item(db):
    return db.catalog_write(
        "items",
        {
            "name": "Test shirt",
            "category_id": db.get_categories()[0]["id"],
            "rate": 100,
            "stock_qty": 20,
            "gst_rate": 5,
        },
    )


def payload(db, item, **kw):
    row = db.get_item_by_id(item)
    return {
        "items": [
            {
                "item_id": item,
                "version": row["version"],
                "quantity": 1,
                "rate": row["rate"],
                "gst_rate": row["gst_rate"],
                "discount": 0,
            }
        ],
        **kw,
    }


def key():
    return str(uuid.uuid4())
