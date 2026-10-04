"""Explicit, offline restore; verified online backup. No reset operation."""

import argparse
import sqlite3
from pathlib import Path
from .config import Settings
from .database import Database
from .lock import server_lock


def restore_backup(settings, source):
    source = Path(source).resolve()
    if source == settings.db_path.resolve():
        raise ValueError("Choose a backup, not the live database")
    if not source.is_file():
        raise ValueError("Backup does not exist")
    with server_lock(settings.db_path):
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup failed integrity check")
            names = {
                r[0]
                for r in src.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not {"bills", "bill_items", "customers", "items"}.issubset(names):
                raise ValueError("Not a billing database backup")
            db = Database(settings.db_path, settings.backup_dir)
            safety = db.backup_database(reason="before-restore")
            with sqlite3.connect(settings.db_path) as dst:
                src.backup(dst)
        # Reapply only supported versioned migrations, without modifying the backup.
        Database(settings.db_path, settings.backup_dir)
        return safety


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["backup", "restore", "check"])
    parser.add_argument("source", nargs="?")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for restore after stopping the server",
    )
    args = parser.parse_args()
    s = Settings()
    if args.operation == "restore":
        if not args.source or not args.confirm:
            parser.error(
                "Restore requires backup path and --confirm; stop the server first"
            )
        print(
            "Restored. Previous live data retained at", restore_backup(s, args.source)
        )
    elif args.operation == "backup":
        print(Database(s.db_path, s.backup_dir).backup_database(reason="manual"))
    else:
        with sqlite3.connect(s.db_path.as_uri() + "?mode=ro", uri=True) as c:
            result = c.execute("PRAGMA integrity_check").fetchall()
            print(result)
            if result != [("ok",)]:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
