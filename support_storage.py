"""Additive storage for support requests, separate from the order workflow."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite

import storage


@asynccontextmanager
async def connection():
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        yield db


def now():
    return datetime.now(timezone.utc).isoformat()


async def init_schema():
    async with connection() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('help', 'bug')),
                status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'in_progress', 'closed')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER NOT NULL REFERENCES support_tickets(id),
                request_id TEXT NOT NULL UNIQUE,
                actor_id INTEGER NOT NULL,
                is_admin INTEGER NOT NULL CHECK(is_admin IN (0, 1)),
                text TEXT NOT NULL,
                photo_id TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS support_tickets_requester ON support_tickets(requester_id, id);
            CREATE INDEX IF NOT EXISTS support_messages_ticket ON support_messages(ticket_id, id);
        """)


async def create_ticket(requester_id, kind, request_id, text, photo_id):
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT ticket_id FROM support_messages WHERE request_id=? AND actor_id=? AND is_admin=0",
            (request_id, requester_id),
        ) as cursor:
            previous = await cursor.fetchone()
        if previous:
            return previous["ticket_id"], False
        cursor = await db.execute(
            "INSERT INTO support_tickets(requester_id,kind,created_at) VALUES (?,?,?)",
            (requester_id, kind, now()),
        )
        ticket_id = cursor.lastrowid
        await db.execute(
            """INSERT INTO support_messages(ticket_id,request_id,actor_id,is_admin,text,photo_id,created_at)
               VALUES (?,?,?,0,?,?,?)""",
            (ticket_id, request_id, requester_id, text, photo_id, now()),
        )
        await db.commit()
        return ticket_id, True


async def ticket(ticket_id, requester_id=None):
    async with connection() as db:
        async with db.execute(
            "SELECT * FROM support_tickets WHERE id=? AND (? IS NULL OR requester_id=?)",
            (ticket_id, requester_id, requester_id),
        ) as cursor:
            return await cursor.fetchone()


async def list_tickets(*, requester_id=None, status=None, offset=0):
    async with connection() as db:
        async with db.execute(
            """SELECT * FROM support_tickets
               WHERE (? IS NULL OR requester_id=?) AND (? IS NULL OR status=?)
               ORDER BY id DESC LIMIT 9 OFFSET ?""",
            (requester_id, requester_id, status, status, offset),
        ) as cursor:
            return await cursor.fetchall()


async def history(ticket_id, offset=0):
    async with connection() as db:
        async with db.execute(
            """SELECT * FROM support_messages WHERE ticket_id=?
               ORDER BY id DESC LIMIT 7 OFFSET ?""",
            (ticket_id, offset),
        ) as cursor:
            return await cursor.fetchall()


async def change_status(ticket_id, status):
    if status not in ("in_progress", "closed"):
        raise ValueError("Invalid support status")
    async with connection() as db:
        cursor = await db.execute(
            """UPDATE support_tickets SET status=? WHERE id=?
               AND (status='new' OR (status='in_progress' AND ?='closed'))""",
            (status, ticket_id, status),
        )
        await db.commit()
        return cursor.rowcount == 1


async def add_reply(ticket_id, admin_id, request_id, text):
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """INSERT INTO support_messages(ticket_id,request_id,actor_id,is_admin,text,created_at)
               SELECT id,?,?,1,?,? FROM support_tickets WHERE id=? AND status!='closed'
               ON CONFLICT(request_id) DO NOTHING""",
            (request_id, admin_id, text, now(), ticket_id),
        )
        if not cursor.rowcount:
            return False
        await db.execute("UPDATE support_tickets SET status='in_progress' WHERE id=? AND status='new'", (ticket_id,))
        await db.commit()
        return True
