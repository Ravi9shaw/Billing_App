import hashlib
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from billing.config import Settings, hash_password, load_env
from billing.setup import save_settings
from billing.server import create_app
from billing.host import Host
from billing.version import API_VERSION, BUILD_ID
from .conftest import payload, key
from .test_api import login


@pytest.fixture
def editable(tmp_path, monkeypatch):
    monkeypatch.setenv("BILLING_DATA_DIR", str(tmp_path))
    for k in (
        "COMPANY_NAME",
        "ADMIN_PASSWORD_HASH",
        "WHATSAPP_PROVIDER",
        "WHATSAPP_TOKEN",
    ):
        monkeypatch.delenv(k, raising=False)
    save_settings(
        {
            "COMPANY_NAME": "Test company",
            "ADMIN_PASSWORD_HASH": hash_password("test-password-123"),
            "WHATSAPP_PROVIDER": "disabled",
        }
    )
    return Settings()


def app_client(s):
    app = create_app(s)
    c = TestClient(app)
    c.headers["X-Billing-Request"] = "1"
    return app, c


def test_env_reload_and_explicit_environment_precedence(editable, monkeypatch):
    import os

    assert "COMPANY_NAME" not in os.environ
    save_settings({"COMPANY_NAME": "Updated company", "COMPANY_ADDRESS": ""}, editable)
    assert Settings().company["name"] == "Updated company"
    assert Settings().company["address"] == ""
    monkeypatch.setenv("COMPANY_NAME", "External name")
    assert Settings().company["name"] == "External name"
    assert "COMPANY_NAME" in Settings().environment_overrides


def test_configuration_permissions_validation_restart_and_saved_invoices(editable):
    app, c = app_client(editable)
    db = app.state.db
    iid = db.catalog_write("items", {"name": "Shirt", "category_id": 1, "rate": 100})
    bid = db.create_bill(payload(db, iid, paid_now=0), "employee", editable, key())[
        "bill_id"
    ]
    assert c.get("/api/admin/configuration").status_code == 401
    login(c, "employee")
    assert c.get("/api/admin/configuration").status_code == 403
    assert c.post("/api/server/restart").status_code == 403
    login(c)
    before = c.get("/api/admin/configuration").json()
    assert (
        "ADMIN_PASSWORD_HASH" not in before["values"]
        and "WHATSAPP_TOKEN" not in before["values"]
    )
    request = {
        "revision": before["revision"],
        "values": {
            "COMPANY_NAME": "Updated shop",
            "UPI_ID": "synthetic@invalid",
            "COMPANY_ADDRESS": "",
        },
    }
    assert (
        c.post("/api/admin/configuration", json=request).status_code == 409
    )  # external server has no restart hook
    assert Settings().company["name"] == "Test company"
    restarted = []
    app.state.restart_callback = lambda: restarted.append(True)
    bad = {**request, "values": {"UPI_ID": "9000000000"}}
    assert c.post("/api/admin/configuration", json=bad).status_code == 400
    assert (
        c.post(
            "/api/admin/configuration",
            json={**request, "values": {"BILLING_DB_PATH": "elsewhere"}},
        ).status_code
        == 400
    )
    assert (
        c.post(
            "/api/admin/configuration", json={**request, "revision": "0" * 64}
        ).status_code
        == 409
    )
    r = c.post("/api/admin/configuration", json=request)
    assert r.status_code == 200, r.text
    assert restarted and r.json()["previous_instance"] == app.state.instance_id
    assert c.post("/api/logout").status_code == 503
    fresh = Settings()
    assert (
        fresh.company["name"] == "Updated shop"
        and fresh.company["upi_id"] == "synthetic@invalid"
    )
    bill, _ = db.get_bill(bid)
    assert bill["company"]["name"] == "Test company" and bill["balance"] == 105
    assert list(editable.backup_dir.glob("*_before-configuration.db"))
    app2, c2 = app_client(fresh)
    login(c2)
    assert c2.get("/api/reports/overview").json()["totals"]["bill_count"] == 1
    with db._conn() as conn:
        audit = conn.execute(
            "SELECT snapshot FROM audit_log WHERE action='configuration-update'"
        ).fetchone()[0]
    assert "synthetic@invalid" not in audit and "COMPANY_NAME" in audit


def test_password_change_invalidates_previous_sessions_after_restart(editable):
    app, c = app_client(editable)
    login(c)
    old_cookie = c.cookies.get("billing_session")
    body = {
        "revision": c.get("/api/admin/configuration").json()["revision"],
        "values": {},
        "new_password": "updated-password-123",
        "confirm_password": "updated-password-123",
        "current_password": "wrong",
    }
    app.state.restart_callback = lambda: None
    assert c.post("/api/admin/configuration", json=body).status_code == 403
    body["current_password"] = "test-password-123"
    assert c.post("/api/admin/configuration", json=body).status_code == 200
    _, new = app_client(Settings())
    new.cookies.set("billing_session", old_cookie)
    assert new.get("/api/session").status_code == 401
    assert (
        new.post(
            "/api/login", json={"role": "admin", "password": "test-password-123"}
        ).status_code
        == 403
    )
    assert (
        new.post(
            "/api/login", json={"role": "admin", "password": "updated-password-123"}
        ).status_code
        == 200
    )
    assert "updated-password-123" not in editable.env_path.read_text()


def test_optional_meta_secret_and_environment_lock(editable, monkeypatch):
    save_settings({"WHATSAPP_TOKEN": "synthetic-private-token"}, editable)
    app, c = app_client(Settings())
    login(c)
    app.state.restart_callback = lambda: None
    config = c.get("/api/admin/configuration").json()
    assert config["whatsapp_token_set"] and "synthetic-private-token" not in json.dumps(
        config
    )
    r = c.post(
        "/api/admin/configuration",
        json={"revision": config["revision"], "values": {"COMPANY_PHONE": ""}},
    )
    assert r.status_code == 200
    assert load_env(editable.env_path)["WHATSAPP_TOKEN"] == "synthetic-private-token"
    monkeypatch.setenv("COMPANY_NAME", "External")
    app, c = app_client(Settings())
    login(c)
    app.state.restart_callback = lambda: None
    config = c.get("/api/admin/configuration").json()
    assert "COMPANY_NAME" in config["locked"]
    assert (
        c.post(
            "/api/admin/configuration",
            json={
                "revision": config["revision"],
                "values": {"COMPANY_NAME": "Overridden"},
            },
        ).status_code
        == 400
    )


def test_host_rejects_legacy_wrong_store_and_stale_build(editable, monkeypatch):
    @contextmanager
    def response(_url, timeout):
        import io

        yield io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr("urllib.request.urlopen", response)
    data = {"application": "cloth-shop-billing", "api_version": 2}
    host = Host(editable)
    assert host.probe()[0] == "legacy"
    assert host.start()[0] == "legacy" and host.child is None
    data.update(api_version=API_VERSION, storage_id="wrong", build=BUILD_ID)
    assert host.probe()[0] == "wrong_data"
    data["storage_id"] = hashlib.sha256(str(editable.db_path).encode()).hexdigest()
    data["build"] = "old"
    assert host.probe()[0] == "outdated"
    data["build"] = BUILD_ID
    assert host.probe()[0] == "ready"
    data["stale_files"] = True
    assert host.probe()[0] == "outdated"
    data["application"] = "other"
    assert host.probe()[0] == "occupied"


def test_health_exposes_route_capabilities_and_running_build(editable, monkeypatch):
    _, c = app_client(editable)
    health = c.get("/api/health").json()
    assert health["api_version"] == 3
    assert {"reports", "configuration", "customer_profiles", "connection"} <= set(
        health["capabilities"]
    )
    assert health["build"] == BUILD_ID and not health["stale_files"]
    monkeypatch.setattr("billing.server.source_build", lambda: "changed")
    assert c.get("/api/health").json()["stale_files"]


def test_software_mode_overrides_vulkan_selection(editable, monkeypatch):
    from billing.desktop import configure_rendering

    for k, v in [
        ("QSG_RHI_BACKEND", "vulkan"),
        ("QT_QUICK_BACKEND", "rhi"),
        ("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-features=Other --custom"),
    ]:
        monkeypatch.setenv(k, v)
    configure_rendering({"BILLING_SOFTWARE_RENDERING": "true"})
    import os

    assert os.environ["QSG_RHI_BACKEND"] == "opengl"
    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert "--disable-features=Other,Vulkan" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "--custom" in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    monkeypatch.setenv("QSG_RHI_BACKEND", "vulkan")
    configure_rendering({"BILLING_SOFTWARE_RENDERING": "false"})
    assert os.environ["QSG_RHI_BACKEND"] == "vulkan"


def test_frozen_host_uses_persistent_working_directory(editable, monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(Host, "probe", lambda _: ("offline", "Offline"))
    calls = []

    def spawn(command, **options):
        calls.append((command, options))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr("subprocess.Popen", spawn)
    host = Host(editable)
    assert host.start()[0] == "starting"
    command, options = calls[0]
    assert command == [sys.executable, "--run-server"]
    assert options["cwd"] == str(editable.data_dir)
    assert options["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert host.start()[0] == "starting" and len(calls) == 1


def test_configuration_rejects_busy_new_port_without_saving(editable):
    import socket

    app, c = app_client(editable)
    app.state.restart_callback = lambda: None
    login(c)
    before = editable.env_path.read_bytes()
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        listener.listen()
        r = c.post(
            "/api/admin/configuration",
            json={
                "revision": c.get("/api/admin/configuration").json()["revision"],
                "values": {"BILLING_PORT": str(listener.getsockname()[1])},
            },
        )
    assert r.status_code == 400 and "unavailable" in r.json()["detail"]
    assert editable.env_path.read_bytes() == before


def test_frozen_fingerprint_includes_archived_python_code(tmp_path, monkeypatch):
    import sys
    from billing.version import source_build

    executable = tmp_path / "Billing.exe"
    executable.write_bytes(b"first packaged Python code")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    before = source_build()
    assert source_build() == before
    executable.write_bytes(b"updated packaged Python code with identical web files")
    assert source_build() != before


def test_launcher_holds_database_lock_before_initialization(editable, monkeypatch):
    from billing import launcher
    from types import SimpleNamespace

    locked = []

    @contextmanager
    def lock(path):
        assert path == editable.db_path
        locked.append(True)
        try:
            yield
        finally:
            locked.clear()

    def application(settings):
        assert locked, "Database initialization must happen under the process lock"
        return SimpleNamespace(state=SimpleNamespace())

    monkeypatch.setattr(launcher, "Settings", lambda: editable)
    monkeypatch.setattr(launcher, "configure_logging", lambda _: None)
    monkeypatch.setattr("billing.lock.server_lock", lock)
    monkeypatch.setattr("billing.server.create_app", application)
    monkeypatch.setattr("uvicorn.Config", lambda *a, **kw: None)
    monkeypatch.setattr("uvicorn.Server", lambda _: SimpleNamespace(run=lambda: None))
    launcher.run_server()
    assert not locked
