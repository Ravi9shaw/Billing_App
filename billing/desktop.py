"""Thin native window: desktop and browser run the same authenticated application."""

import os
import sys
import time
from pathlib import Path
from .config import Settings, hash_password


def configure_rendering(values=None):
    values = values or os.environ
    if values.get("BILLING_SOFTWARE_RENDERING", "true").lower() == "true":
        # Force both Qt's RHI selection and Chromium's renderer out of Vulkan.
        # QT_QUICK_BACKEND alone does not choose Qt WebEngine's RHI backend.
        os.environ["QT_QUICK_BACKEND"] = "software"
        os.environ["QSG_RHI_BACKEND"] = "opengl"
        os.environ["QT_OPENGL"] = "software"
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        if "--disable-gpu" not in flags.split():
            flags += " --disable-gpu"
        import re

        found = re.search(r"--disable-features=([^ ]+)", flags)
        if found:
            features = found.group(1).split(",")
            if "Vulkan" not in features:
                flags = flags.replace(found.group(0), found.group(0) + ",Vulkan")
        else:
            flags += " --disable-features=Vulkan"
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = flags.strip()


def main():
    s = Settings()
    configure_rendering(s.values)
    from PySide6.QtCore import QUrl, QTimer
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (
        QApplication,
        QMainWindow,
        QMessageBox,
        QDialog,
        QFormLayout,
        QLineEdit,
        QDialogButtonBox,
        QFileDialog,
        QInputDialog,
        QLabel,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtPrintSupport import QPrinter, QPrintDialog

    app = QApplication(sys.argv)
    app.setApplicationName("Shop Billing")
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
        upi_phone = QLineEdit(s.company.get("upi_phone", ""))
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
            ("UPI-linked phone (optional reference)", upi_phone),
            ("Receiving UPI ID from payment app (optional)", upi),
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
                    "UPI_PHONE": upi_phone.text().strip(),
                    "ADMIN_PASSWORD_HASH": hash_password(password.text()),
                    "WHATSAPP_PROVIDER": "meta" if token.text() else "disabled",
                    "WHATSAPP_TOKEN": token.text(),
                    "WHATSAPP_PHONE_NUMBER_ID": phone.text(),
                    "WHATSAPP_BILL_TEMPLATE": template.text(),
                }
                save_settings(values, s)
                dialog.accept()
            except ValueError as e:
                QMessageBox.warning(dialog, "Check setup", str(e))

        buttons.accepted.connect(save)
        if dialog.exec() != QDialog.Accepted:
            return
        s = Settings()
    from .host import Host

    host = Host(s)

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
    toolbar = window.addToolBar("Server controls")
    toolbar.setMovable(False)
    connect_action = toolbar.addAction("Start / reconnect server")
    restart_action = toolbar.addAction("Restart local server")
    browser_action = toolbar.addAction("Open in browser")
    logs_action = toolbar.addAction("Open logs")
    status_label = QLabel("Checking backend…")
    toolbar.addWidget(status_label)
    window.show()
    timer = QTimer()
    timer.setInterval(800)
    started = time.monotonic()

    def explain(message):
        from html import escape

        view.setHtml(
            '<html><body style="font:16px sans-serif;padding:35px;background:#f5f7f5"><h2>Billing server connection</h2><p>'
            + escape(message)
            + "</p><p>Use the controls above to start/reconnect, restart a compatible host, open logs, or use your normal browser. Your database has not been reset.</p><p>Database: "
            + escape(str(host.settings.db_path))
            + "</p></body></html>"
        )
        status_label.setText(message[:100])

    def check():
        state, message = host.probe()
        status_label.setText(message[:100])
        if state == "ready":
            timer.stop()
            view.load(QUrl(host.url))
        elif state not in ("offline", "starting"):
            timer.stop()
            explain(message)
        elif host.child and host.child.poll() is not None:
            timer.stop()
            explain(
                f"The backend exited with code {host.child.returncode}. Open logs to see the startup error. If another older server holds the database lock, stop that host first, then reconnect."
            )
        elif time.monotonic() - started > 45:
            timer.stop()
            explain(
                message
                + " Startup did not complete. Check the server address and launcher/server logs, then use Start / reconnect."
            )

    def connect_host():
        nonlocal host, started
        timer.stop()
        try:
            host = Host(Settings())
            state, message = host.start()
            explain(message)
            started = time.monotonic()
            restart_action.setEnabled(not host.settings.server_url)
            timer.start()
        except Exception as exc:
            explain(str(exc))

    def restart_host():
        password, accepted = QInputDialog.getText(
            window, "Restart backend", "Admin password", QLineEdit.Password
        )
        if not accepted:
            return
        try:
            host.restart(password)

            def wait_for_restart():
                nonlocal host, started
                host = Host(Settings())
                started = time.monotonic()
                timer.start()

            QTimer.singleShot(1800, wait_for_restart)
            explain("Restart requested. Waiting for active work and backups to finish…")
        except Exception as exc:
            QMessageBox.warning(window, "Restart could not complete", str(exc))

    connect_action.triggered.connect(connect_host)
    restart_action.triggered.connect(restart_host)
    browser_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(host.url)))
    logs_action.triggered.connect(
        lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(host.settings.data_dir / "logs"))
        )
    )
    timer.timeout.connect(check)
    QTimer.singleShot(0, connect_host)
    sys.exit(app.exec())
