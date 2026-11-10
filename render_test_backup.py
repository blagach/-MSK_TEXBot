"""Backup and restore-candidate commands for the isolated test disk."""
import argparse
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path


def validate_database(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("SQLite foreign key check failed")
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


def verify_preserved_rows(snapshot_path: Path, candidate_path: Path) -> int:
    with closing(open_read_only(snapshot_path)) as snapshot_db, closing(open_read_only(candidate_path)) as candidate_db:
        validate_database(snapshot_db)
        validate_database(candidate_db)
        tables = [row[0] for row in snapshot_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        missing = object()
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            columns = ", ".join('"' + row[1].replace('"', '""') + '"'
                                for row in snapshot_db.execute(f"PRAGMA table_info({quoted})"))
            query = f"SELECT {columns} FROM {quoted} ORDER BY {columns}"
            if any(old != new for old, new in zip_longest(snapshot_db.execute(query), candidate_db.execute(query),
                                                          fillvalue=missing)):
                raise RuntimeError(f"SQLite migration changed pre-existing rows in {name}")
        return len(tables)


def restore_candidate(backup: Path, digest: str) -> Path:
    with backup.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != digest:
            raise RuntimeError("Backup checksum mismatch")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = backup.parent / f"restore-candidate-{timestamp}.sqlite"
    backup_database(backup, candidate)
    verify_preserved_rows(backup, candidate)
    return candidate


def verify_small_snapshot(path: Path) -> tuple[int, int] | None:
    """Restore a small test snapshot into an independent in-memory database."""
    if path.stat().st_size > 16 * 1024 * 1024:
        return None
    with closing(open_read_only(path)) as original, closing(sqlite3.connect(":memory:")) as restored:
        original.backup(restored)
        validate_database(restored)
        counts = tuple(restored.execute(
            "SELECT (SELECT count(*) FROM leads), (SELECT count(*) FROM offers)"
        ).fetchone())
        expected = original.execute(
            "SELECT (SELECT count(*) FROM leads), (SELECT count(*) FROM offers)"
        ).fetchone()
        if counts != expected:
            raise RuntimeError("Restored test counts do not match the snapshot")
        return counts


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
