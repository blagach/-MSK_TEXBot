"""Single SQLite database location and safe one-time migration."""
import os
import sqlite3

# Render persistent disk is mounted at /var/data.
DB_PATH = "/var/data/bot.db"
LEGACY_DB_PATH = "bot.db"


def prepare_database() -> None:
    """Prepare the persistent DB without deleting or overwriting any file.

    If only the legacy working-directory DB exists, copy it using SQLite's
    backup API (which also handles an active WAL safely). If both files exist,
    stop explicitly so data can be reconciled rather than silently choosing
    one and hiding records.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    legacy = os.path.abspath(LEGACY_DB_PATH)
    persistent = os.path.abspath(DB_PATH)

    if legacy == persistent or not os.path.exists(legacy):
        return

    if os.path.exists(persistent):
        raise RuntimeError(
            "Both legacy bot.db and persistent /var/data/bot.db exist. "
            "Automatic migration was skipped to protect existing data; "
            "the databases must be reconciled before startup."
        )

    with sqlite3.connect(legacy) as source, sqlite3.connect(persistent) as target:
        source.backup(target)
