"""Backup and restore-candidate commands for the isolated test disk."""
import argparse
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def validate_database(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("SQLite integrity check failed")
    connection.execute("SELECT id FROM leads LIMIT 0")


def open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)


def backup_database(source: Path, destination: Path) -> str:
    if not source.is_file() or source.is_symlink():
        raise ValueError("Source must be an existing SQLite file")
    with closing(open_read_only(source)) as original:
        destination.touch(mode=0o600, exist_ok=False)
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)
            validate_database(copy)
    with destination.open("rb") as copy_file:
        return hashlib.file_digest(copy_file, "sha256").hexdigest()


def snapshot(database: Path) -> tuple[Path, str]:
    backups = database.parent / "backups"
    if backups.is_symlink():
        raise ValueError("Backups directory must not be a symlink")
    backups.mkdir(mode=0o700, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backups / f"test-{timestamp}.sqlite"
    return destination, backup_database(database, destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("snapshot", "restore-candidate"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    from render_test_start import test_database_path

    database = test_database_path()
    if args.operation == "snapshot":
        destination, digest = snapshot(database)
    else:
        if args.backup is None or args.sha256 is None or args.backup.is_symlink():
            parser.error("restore-candidate requires --backup and --sha256")
        with args.backup.open("rb") as backup:
            if hashlib.file_digest(backup, "sha256").hexdigest() != args.sha256:
                parser.error("Backup checksum mismatch")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = database.parent / f"restore-candidate-{timestamp}.sqlite"
        digest = backup_database(args.backup, destination)
    print(f"{destination} SHA-256 {digest}")


if __name__ == "__main__":
    main()
