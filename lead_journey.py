"""Additive fields and lifecycle history for customer requests."""
import aiosqlite
import json


OFFER_RESULT = """CASE WHEN e.offer_id=o.id THEN 'Выбран'
                       WHEN l.status IN ('closed','completed','cancelled','rejected') THEN 'Заявка закрыта'
                       WHEN l.status IN ('archived','archive') THEN 'Заявка в архиве'
                       ELSE 'Подан' END"""


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
            CREATE TABLE IF NOT EXISTS lead_attachments (
                lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
                selected_json TEXT NOT NULL,
                custom_text TEXT NOT NULL
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


def attachments_text(data):
    selected = data.get("attachments")
    if selected is None:
        selected = json.loads(data.get("attachments_json") or "[]")
    custom = data.get("attachments_custom") or ""
    return ", ".join([*selected, *([custom] if custom else [])])


async def write_details(db, lead_id, data, username):
    await db.execute("INSERT INTO lead_work_details VALUES (?, ?, ?, ?, ?)",
                     (lead_id, data.get("equipment_name"), data.get("map_link"), data.get("schedule"), username))


async def write_attachments(db, lead_id, data):
    selected = data.get("attachments", [])
    custom = data.get("attachments_custom", "")
    if not isinstance(selected, list) or any(not isinstance(value, str) or not 1 <= len(value) <= 120 for value in selected):
        raise ValueError("Invalid attachments")
    if not isinstance(custom, str) or len(custom) > 300:
        raise ValueError("Invalid attachment description")
    await db.execute("INSERT INTO lead_attachments VALUES (?,?,?)",
                     (lead_id, json.dumps(list(dict.fromkeys(selected)), ensure_ascii=False), custom))


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
    if reason is not None:
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("Укажите причину закрытия длиной от 1 до 500 символов")
        reason = reason.strip()
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
