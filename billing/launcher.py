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
    url = f"http://{lan_ip()}:{settings.port}"
    try:
        import qrcode

        qrcode.make(url).save(settings.data_dir / "server_qr.png")
    except Exception:
        logging.exception("Connection QR generation failed")
    logging.info("Starting billing server at %s; database %s", url, settings.db_path)
    from .lock import server_lock

    # No formatter references absent console streams in a Windows windowed executable.
    with server_lock(settings.db_path):
        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=40,
        )
