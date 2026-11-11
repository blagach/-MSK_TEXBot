"""Additive SQLite schema and atomic operations; no Telegram requests here."""
import re
from datetime import datetime, timezone

import aiosqlite

import lead_journey

DB_PATH = "/var/data/bot.db"
ALLOWED_TRANSITIONS = {("new", "publishing"), ("publishing", "new"), ("new", "rejected")}


def parse_id(value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    suffix = value[len(prefix):]
    if not re.fullmatch(r"[1-9][0-9]{0,17}", suffix):
        return None
    return int(suffix)


def valid_text(value, limit, phone=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    if phone:
        return bool(re.fullmatch(r"(?:\+7|8)[0-9]{10}", value.strip()))
    return True


async def init_schema():
    import user_access
    await user_access.init_schema()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            provider_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            username TEXT NOT NULL,
            price TEXT NOT NULL,
            comment TEXT NOT NULL,
            phone TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(lead_id, provider_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS lead_publications (
            lead_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL
        )""")
        await db.execute("CREATE TABLE IF NOT EXISTS bot_visitors (telegram_id INTEGER PRIMARY KEY)")
        await db.execute("""CREATE TABLE IF NOT EXISTS offer_notes (
            offer_id INTEGER PRIMARY KEY REFERENCES offers(id),
            note TEXT NOT NULL
        )""")
        await db.commit()
    await lead_journey.init_schema(DB_PATH)


async def record_visitor(telegram_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO bot_visitors VALUES (?) ON CONFLICT DO NOTHING", (telegram_id,))
        await db.commit()


async def transition(lead_id, expected, target):
    if (expected, target) not in ALLOWED_TRANSITIONS:
        raise ValueError("Unsupported status transition")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE leads SET status = ? WHERE id = ? AND status = ?",
            (target, lead_id, expected),
        )
        if cursor.rowcount == 1 and target == "rejected":
            await lead_journey.record_event(db, lead_id, "rejected")
        await db.commit()
        return cursor.rowcount == 1


async def finish_publication(lead_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE leads SET status = 'published' WHERE id = ? AND status = 'publishing'", (lead_id,)
        )
        if cursor.rowcount != 1:
            raise ValueError("Lead is not awaiting publication")
        await db.execute("INSERT INTO lead_publications VALUES (?, ?, ?)", (lead_id, chat_id, message_id))
        await lead_journey.record_event(db, lead_id, "published")
        await db.commit()


async def save_offer(lead_id, provider_id, name, username, price, comment, phone, *, note=None):
    import user_access
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        if not await user_access.allowed_in(db,provider_id,"offers"):
            raise user_access.AccessDenied("Отправка откликов ограничена")
        cursor = await db.execute("SELECT status FROM leads WHERE id = ?", (lead_id,))
        lead = await cursor.fetchone()
        if not lead or lead[0] != "published":
            return None
        cursor = await db.execute(
            """INSERT INTO offers(lead_id, provider_id, provider_name, username, price, comment, phone, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(lead_id, provider_id) DO NOTHING""",
            (lead_id, provider_id, name, username, price, comment, phone, datetime.now(timezone.utc).isoformat()),
        )
        if cursor.rowcount != 1:
            return None
        offer_id = cursor.lastrowid
        if note:
            await db.execute("INSERT INTO offer_notes (offer_id, note) VALUES (?, ?)", (offer_id, note))
        await db.commit()
        return offer_id


async def has_offer(lead_id, provider_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM offers WHERE lead_id=? AND provider_id=?", (lead_id, provider_id)) as cursor:
            return await cursor.fetchone() is not None


async def offer_phone_for_customer(offer_id, customer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT offers.phone FROM offers JOIN leads ON leads.id = offers.lead_id
               WHERE offers.id = ? AND leads.customer_id = ?""",
            (offer_id, customer_id),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def mark_offer(offer_id, status):
    if status not in {"sent", "unconfirmed"}:
        raise ValueError("Unsupported delivery status")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE offers SET delivery_status = ? WHERE id = ?", (status, offer_id))
        await db.commit()
