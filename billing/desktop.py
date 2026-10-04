"""Thin native window: desktop and browser run the same authenticated application."""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from .config import Settings, hash_password


def main():
    from PySide6.QtCore import QUrl, QTimer
    from PySide6.QtWidgets import (
        QApplication,
        QMainWindow,
        QMessageBox,
        QDialog,
        QFormLayout,
        QLineEdit,
        QDialogButtonBox,
        QFileDialog,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtPrintSupport import QPrinter, QPrintDialog

    app = QApplication(sys.argv)
    app.setApplicationName("Shop Billing")
    s = Settings()
    if not s.admin_hash and not s.server_url:
        from .setup import save_settings

        dialog = QDialog()
        dialog.setWindowTitle("First setup")
        form = QFormLayout(dialog)
        name = QLineEdit(s.company["name"])
        password = QLineEdit()
        password.setEchoMode(QLineEdit.Password)
        confirm = QLineEdit()
        confirm.setEchoMode(QLineEdit.Password)
        upi = QLineEdit(s.company.get("upi_id", ""))
        upi.setPlaceholderText("yourshop@bank")
        token = QLineEdit()
        token.setEchoMode(QLineEdit.Password)
        phone = QLineEdit()
        template = QLineEdit()
        for label, field in [
            ("Company name", name),
            ("Admin password (8+ characters)", password),
            ("Confirm password", confirm),
            ("Receiving UPI ID (optional)", upi),
            ("WhatsApp API token (optional)", token),
            ("WhatsApp phone number ID (optional)", phone),
            ("Approved invoice template (optional)", template),
        ]:
            form.addRow(label, field)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        form.addRow(buttons)
        buttons.rejected.connect(dialog.reject)

        def save():
            try:
                if not name.text().strip():
                    raise ValueError("Company name is required")
                if password.text() != confirm.text():
                    raise ValueError("Passwords do not match")
                values = {
                    "COMPANY_NAME": name.text().strip(),
                    "UPI_ID": upi.text().strip(),
                    "ADMIN_PASSWORD_HASH": hash_password(password.text()),
                    "WHATSAPP_PROVIDER": "meta" if token.text() else "disabled",
                    "WHATSAPP_TOKEN": token.text(),
                    "WHATSAPP_PHONE_NUMBER_ID": phone.text(),
                    "WHATSAPP_BILL_TEMPLATE": template.text(),
                }
                save_settings(values, s)
                os.environ.update(values)
                dialog.accept()
            except ValueError as e:
                QMessageBox.warning(dialog, "Check setup", str(e))

        buttons.accepted.connect(save)
        if dialog.exec() != QDialog.Accepted:
            return
        s = Settings()
    url = s.server_url or f"http://127.0.0.1:{s.port}"

    def ready():
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=0.5) as r:
                data = json.load(r)
                return (
                    data.get("application") == "cloth-shop-billing"
                    and data.get("api_version") == 2
                )
        except Exception:
            return False

    if not ready() and not s.server_url:
        logdir = s.data_dir / "logs"
        logdir.mkdir(exist_ok=True)
        command = (
            [sys.executable, "--run-server"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "billing", "--run-server"]
        )
        flags = (
            (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
            if os.name == "nt"
            else 0
        )
        with open(logdir / "launcher.log", "ab") as log:
            subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=flags,
                start_new_session=os.name != "nt",
                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
            )

    # The server intentionally survives desktop closure so browser tills remain connected.
    class View(QWebEngineView):
        windows = []

        def __init__(self):
            super().__init__()
            self.printer = None
            self.page().printRequested.connect(self.print_page)
            profile = self.page().profile()
            if not profile.property("billing_download_connected"):
                profile.downloadRequested.connect(self.download)
                profile.setProperty("billing_download_connected", True)

        def createWindow(self, _):
            view = View()
            view.resize(1000, 850)
            view.show()
            self.windows.append(view)
            return view

        def print_page(self):
            self.printer = QPrinter(QPrinter.HighResolution)
            dialog = QPrintDialog(self.printer, self)
            if dialog.exec() == QDialog.Accepted:
                self.print(self.printer)

        def download(self, request):
            filename, _ = QFileDialog.getSaveFileName(
                self, "Save file", request.downloadFileName()
            )
            if filename:
                request.setDownloadDirectory(str(Path(filename).parent))
                request.setDownloadFileName(Path(filename).name)
                request.accept()
            else:
                request.cancel()

    window = QMainWindow()
    view = View()
    window.setCentralWidget(view)
    window.resize(1360, 900)
    window.setWindowTitle(s.company["name"] + " · Billing")
    view.setHtml(
        '<html><body style="font:20px Segoe UI;padding:60px;background:#f5f7f5"><h2>Starting your shop…</h2><p>Connecting to the shared billing server.</p></body></html>'
    )
    window.show()
    started = time.monotonic()
    timer = QTimer()
    timer.setInterval(350)

    def check():
        if ready():
            timer.stop()
            view.load(QUrl(url))
        elif time.monotonic() - started > 40:
            timer.stop()
            QMessageBox.critical(
                window,
                "Server could not start",
                f"Could not reach {url}.\nCheck the server address, port and {s.data_dir / 'logs'}.\nYour existing data has not been reset.",
            )

    timer.timeout.connect(check)
    timer.start()
    sys.exit(app.exec())
