"""Additive fields and lifecycle history for customer requests."""
import aiosqlite


async def init_schema(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS lead_work_details (
                lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
                equipment_name TEXT,
                map_link TEXT,
                schedule TEXT,
                customer_username TEXT
            );
            CREATE TABLE IF NOT EXISTS lead_lifecycle_events (
                id INTEGER PRIMARY KEY,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                actor_id INTEGER,
                action TEXT NOT NULL,
                reason TEXT,
                offer_id INTEGER REFERENCES offers(id),
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS lead_lifecycle_once
                ON lead_lifecycle_events(lead_id, action);
        """)
        await db.commit()


async def write_details(db, lead_id, data, username):
    await db.execute("INSERT INTO lead_work_details VALUES (?, ?, ?, ?, ?)",
                     (lead_id, data.get("equipment_name"), data.get("map_link"), data.get("schedule"), username))


async def record_event(db, lead_id, action, *, actor_id=None, reason=None, offer_id=None):
    from datetime import datetime, timezone

    await db.execute(
        """INSERT INTO lead_lifecycle_events (lead_id, actor_id, action, reason, offer_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (lead_id, actor_id, action, reason, offer_id, datetime.now(timezone.utc).isoformat()),
    )


async def change_lifecycle(db_path, lead_id, actor_id, *, admin=False, reason=None, offer_id=None):
    if (reason is None) == (offer_id is None):
        raise ValueError("Ожидается причина закрытия или номер отклика")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT customer_id, status FROM leads WHERE id=?", (lead_id,)) as cursor:
            lead = await cursor.fetchone()
        if not lead or lead[1] != "published" or (lead[0] != actor_id and (not admin or offer_id is not None)):
            return False
        if offer_id is not None:
            async with db.execute(
                """SELECT 1 FROM offers WHERE id=? AND lead_id=? AND NOT EXISTS (
                   SELECT 1 FROM lead_lifecycle_events WHERE lead_id=? AND action='selected')""",
                (offer_id, lead_id, lead_id),
            ) as cursor:
                if not await cursor.fetchone():
                    return False
            await record_event(db, lead_id, "selected", actor_id=actor_id, offer_id=offer_id)
        else:
            await db.execute("UPDATE leads SET status='closed' WHERE id=?", (lead_id,))
            await record_event(db, lead_id, "closed", actor_id=actor_id, reason=reason)
        await db.commit()
        return True
