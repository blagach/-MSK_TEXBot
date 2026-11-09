"""Additive SQLite schema and atomic operations; no Telegram requests here."""
import re
from datetime import datetime, timezone

import aiosqlite

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
        await db.execute("""CREATE TABLE IF NOT EXISTS waste_lead_details (
            lead_id INTEGER PRIMARY KEY REFERENCES leads(id),
            kind TEXT NOT NULL,
            material TEXT NOT NULL,
            hazard_class TEXT,
            service TEXT NOT NULL,
            volume_liters INTEGER NOT NULL CHECK(volume_liters > 0),
            mass_kg INTEGER CHECK(mass_kg > 0),
            loading TEXT NOT NULL,
            destination TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS lead_rejection_reasons (
            lead_id INTEGER PRIMARY KEY REFERENCES leads(id), reason TEXT NOT NULL
        )""")
        await db.commit()


async def transition(lead_id, expected, target):
    if (expected, target) not in ALLOWED_TRANSITIONS:
        raise ValueError("Unsupported status transition")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE leads SET status = ? WHERE id = ? AND status = ?",
            (target, lead_id, expected),
        )
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
        await db.commit()


async def save_offer(lead_id, provider_id, name, username, price, comment, phone):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT status FROM leads WHERE id = ?", (lead_id,))
        lead = await cursor.fetchone()
        if not lead or lead[0] != "published":
            return None
        cursor = await db.execute(
            """INSERT INTO offers(lead_id, provider_id, provider_name, username, price, comment, phone, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(lead_id, provider_id) DO NOTHING""",
            (lead_id, provider_id, name, username, price, comment, phone, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return cursor.lastrowid if cursor.rowcount == 1 else None


async def has_offer(lead_id, provider_id):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT 1 FROM offers WHERE lead_id=? AND provider_id=?", (lead_id, provider_id)
        )).fetchone()
        return row is not None


async def offer_contact(offer_id, customer_id):
    async with aiosqlite.connect(DB_PATH) as db:
        return await (await db.execute(
            """SELECT o.provider_name, o.phone FROM offers o
               JOIN leads l ON l.id=o.lead_id WHERE o.id=? AND l.customer_id=?""",
            (offer_id, customer_id),
        )).fetchone()


async def reject_with_reason(lead_id, reason):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE leads SET status='rejected' WHERE id=? AND status='new'", (lead_id,)
        )
        if cursor.rowcount != 1:
            return False
        await db.execute("INSERT INTO lead_rejection_reasons VALUES (?,?)", (lead_id, reason))
        await db.commit()
        return True


async def mark_offer(offer_id, status):
    if status not in {"sent", "unconfirmed"}:
        raise ValueError("Unsupported delivery status")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE offers SET delivery_status = ? WHERE id = ?", (status, offer_id))
        await db.commit()
