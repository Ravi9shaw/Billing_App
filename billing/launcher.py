import logging
import socket
from logging.handlers import RotatingFileHandler
from .config import Settings


def configure_logging(settings):
    folder = settings.data_dir / "logs"
    folder.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        folder / "server.log", maxBytes=2000000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def run_server():
    import uvicorn
    from .server import create_app

    settings = Settings()
    configure_logging(settings)
    url = (
        settings.public_url
        or f"http://{lan_ip() if settings.host in ('0.0.0.0', '::') else settings.host}:{settings.port}"
    )
    import sys

    if sys.stdout is not None:
        print(f"Shop server: {url} — connect devices on the same Wi-Fi", flush=True)
    try:
        import qrcode

        qrcode.make(url).save(settings.data_dir / "server_qr.png")
    except Exception:
        logging.exception("Connection QR generation failed")
    logging.info("Starting billing server at %s; database %s", url, settings.db_path)
    from .lock import server_lock

    # No formatter references absent console streams in a Windows windowed executable.
    restart_requested = False
    with server_lock(settings.db_path):
        application = create_app(settings)
        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host=settings.host,
                port=settings.port,
                log_config=None,
                access_log=False,
                timeout_graceful_shutdown=40,
            )
        )

        def restart():
            nonlocal restart_requested
            restart_requested = True
            server.should_exit = True

        application.state.restart_callback = restart
        server.run()
    if restart_requested:
        # A fresh interpreter is essential after updating Python files on disk.
        import subprocess
        import os

        command = (
            [sys.executable, "--run-server"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", "billing", "--run-server"]
        )
        from pathlib import Path

        with open(settings.data_dir / "logs" / "launcher.log", "ab") as output:
            subprocess.Popen(
                command,
                cwd=str(
                    settings.data_dir
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
                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
            )
