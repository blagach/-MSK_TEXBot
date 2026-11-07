"""Durable notification reservations; intentionally no Telegram sends.

Unknown delivery outcomes remain reserved for manual reconciliation.
Never auto-retry a reserved recipient, even after process restart.
"""
import aiosqlite


async def init_schema(db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS provider_notification_reservations (
            lead_id INTEGER NOT NULL,
            provider_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved'
                CHECK(status IN ('reserved','confirmed','uncertain')),
            PRIMARY KEY (lead_id, provider_id)
        )""")
        await db.commit()


async def reserve(lead_id, provider_id, db_path="bot.db"):
    """Reserve once, atomically, only for an already published lead."""
    if not isinstance(lead_id, int) or lead_id <= 0:
        raise ValueError("Invalid lead id")
    if not isinstance(provider_id, int) or provider_id <= 0:
        raise ValueError("Invalid provider id")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("""INSERT OR IGNORE INTO provider_notification_reservations
            (lead_id, provider_id)
            SELECT l.id, p.telegram_id FROM leads l
            JOIN provider_profiles p ON p.telegram_id=?
            WHERE l.id=? AND l.status='published'
              AND p.blocked=0 AND p.notifications_enabled=1""",
            (provider_id, lead_id))
        await db.commit()
        return cursor.rowcount == 1


async def record_outcome(lead_id, provider_id, status, db_path="bot.db"):
    if status not in ("confirmed", "uncertain"):
        raise ValueError("Invalid delivery outcome")
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""UPDATE provider_notification_reservations
            SET status=? WHERE lead_id=? AND provider_id=? AND status='reserved'""",
            (status, lead_id, provider_id))
        await db.commit()
        return cursor.rowcount == 1
