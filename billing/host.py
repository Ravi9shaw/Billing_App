"""Desktop recovery controls do not depend on a working web page."""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import http.cookiejar
from pathlib import Path
from .version import API_VERSION, BUILD_ID


class Host:
    def __init__(self, settings):
        self.settings = settings
        self.url = settings.server_url or f"http://127.0.0.1:{settings.port}"
        self.child = None

    def probe(self):
        try:
            with urllib.request.urlopen(
                self.url + "/api/health", timeout=1
            ) as response:
                data = json.load(response)
        except urllib.error.HTTPError as e:
            return (
                "occupied",
                f"The service at {self.url} returned HTTP {e.code}; it is not a healthy billing host.",
            )
        except (urllib.error.URLError, TimeoutError, OSError):
            return "offline", f"Cannot reach {self.url}."
        except (ValueError, TypeError):
            return (
                "occupied",
                f"The service at {self.url} did not return billing health information.",
            )
        if (
            not isinstance(data, dict)
            or data.get("application") != "cloth-shop-billing"
        ):
            return (
                "occupied",
                f"Another application is using {self.url}. Check the configured port.",
            )
        if data.get("api_version") != API_VERSION:
            return (
                "legacy",
                f"An older billing server (API {data.get('api_version', 'unknown')}) is still running at {self.url}. Stop its terminal with Ctrl+C, or its Python/Billing --run-server process in Task Manager, then click Start / reconnect. Closing only the billing window does not stop that older host.",
            )
        if not self.settings.server_url:
            storage = hashlib.sha256(str(self.settings.db_path).encode()).hexdigest()
            if data.get("storage_id") != storage:
                return (
                    "wrong_data",
                    "The running host uses a different database location. Do not create new bills here. Check Configuration / your BILLING_DATA_DIR settings.",
                )
            if data.get("build") != BUILD_ID or data.get("stale_files"):
                return (
                    "outdated",
                    "The running backend has older code. Use Restart local server, then sign in with the admin password.",
                )
        if data.get("restarting"):
            return (
                "starting",
                "The backend is restarting and preserving committed records.",
            )
        return "ready", f"Connected to {self.url}"

    def start(self):
        state, message = self.probe()
        if state != "offline" or self.settings.server_url:
            return state, message
        if self.child and self.child.poll() is None:
            return "starting", "Waiting for the backend to finish starting."
        logs = self.settings.data_dir / "logs"
        logs.mkdir(exist_ok=True)
        command = (
            [sys.executable, "--run-server"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "billing", "--run-server"]
        )
        with (logs / "launcher.log").open("ab") as output:
            self.child = subprocess.Popen(
                command,
                cwd=str(
                    self.settings.data_dir
                    if getattr(sys, "frozen", False)
                    else Path(__file__).resolve().parent.parent
                ),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
                )
                if os.name == "nt"
                else 0,
                env={
                    **os.environ,
                    "BILLING_DATA_DIR": str(self.settings.data_dir),
                    "PYINSTALLER_RESET_ENVIRONMENT": "1",
                },
            )
        return "starting", "Starting the local backend…"

    def restart(self, password):
        if self.settings.server_url:
            raise ValueError(
                "Use the host computer or its admin Configuration tab to restart a remote backend."
            )
        state, message = self.probe()
        if state not in ("ready", "outdated"):
            raise ValueError(message)
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        for path, body in [
            ("/api/login", {"role": "admin", "password": password}),
            ("/api/server/restart", {}),
        ]:
            request = urllib.request.Request(
                self.url + path,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json", "X-Billing-Request": "1"},
                method="POST",
            )
            try:
                with opener.open(request, timeout=35) as response:
                    json.load(response)
            except urllib.error.HTTPError as e:
                try:
                    detail = json.load(e).get("detail", "Restart failed")
                except (ValueError, TypeError):
                    detail = "Restart failed"
                raise ValueError(detail) from e
