"""Durable notification reservations; intentionally no Telegram sends.

Unknown delivery outcomes remain reserved for manual reconciliation.
Never auto-retry a reserved recipient, even after process restart.
"""
import aiosqlite
import json
from datetime import datetime, timezone


async def cancelled_in(db, lead_id, provider_id):
    return bool(await (await db.execute(
        "SELECT 1 FROM admin_action_log WHERE action='provider_notification_cancelled' "
        "AND target_id=? AND json_extract(details,'$.provider_id')=? LIMIT 1",
        (lead_id, provider_id),
    )).fetchone())


async def phase_in(db, lead_id, provider_id):
    row = await (await db.execute(
        "SELECT action FROM admin_action_log WHERE target_id=? AND action IN "
        "('provider_notification_queued','provider_notification_attempt','provider_notification_rejected',"
        "'provider_notification_cancelled') AND json_extract(details,'$.provider_id')=? ORDER BY id DESC LIMIT 1",
        (lead_id, provider_id),
    )).fetchone()
    return row[0] if row else None


async def audit_delivery_in(db, lead_id, provider_id, action, result, *, actor_id=0, reason=None):
    await db.execute(
        'INSERT INTO admin_action_log(actor_id,action,target_id,result,details,created_at) VALUES (?,?,?,?,?,?)',
        (actor_id, action, lead_id, result,
         json.dumps({'provider_id': provider_id, 'reason': reason}, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()),
    )


async def cancel_in(db, lead_id, provider_id, *, actor_id, reason):
    """Append a terminal cancellation; keep the consumed reservation unchanged.

    The old status CHECK and all historical records remain compatible. A reserved
    row with this audit marker is cancelled, not an unknown delivery outcome.
    Call only before sending, under the shared lead guard and a DB transaction.
    """
    if await cancelled_in(db, lead_id, provider_id):
        return
    await audit_delivery_in(db, lead_id, provider_id, 'provider_notification_cancelled', 'cancelled',
                            actor_id=actor_id, reason=reason)


async def cancel_pending_in(db, lead_id, actor_id):
    for provider_id, in await (await db.execute(
        "SELECT provider_id FROM provider_notification_reservations WHERE lead_id=? AND status='reserved'",
        (lead_id,),
    )).fetchall():
        # A legacy reservation, or an attempt interrupted by process death, is
        # unknown. Never relabel it as definitely unsent or replay it.
        if await phase_in(db, lead_id, provider_id) in {'provider_notification_queued', 'provider_notification_rejected'}:
            await cancel_in(db, lead_id, provider_id, actor_id=actor_id, reason='lead_closed')


async def may_send(lead_id, provider_id, db_path, revision):
    """Final durable gate, also after 429. Guard must remain held through send."""
    import user_access
    import lead_journey
    async with aiosqlite.connect(db_path) as db:
        await db.execute('BEGIN IMMEDIATE')
        reservation = await (await db.execute(
            'SELECT status FROM provider_notification_reservations WHERE lead_id=? AND provider_id=?',
            (lead_id, provider_id),
        )).fetchone()
        if not reservation or reservation[0] != 'reserved' or await cancelled_in(db, lead_id, provider_id):
            return False
        if await phase_in(db, lead_id, provider_id) not in {'provider_notification_queued', 'provider_notification_rejected'}:
            return False  # Legacy/started-but-unconfirmed outcome needs reconciliation.
        lead = await (await db.execute('SELECT status,customer_id FROM leads WHERE id=?', (lead_id,))).fetchone()
        provider = await (await db.execute(
            'SELECT blocked,notifications_enabled FROM provider_profiles WHERE telegram_id=?', (provider_id,),
        )).fetchone()
        eligible = (lead and lead[0] == 'published' and await lead_journey.revision_in(db, lead_id) == revision
                    and provider and not provider[0] and provider[1]
                    and await user_access.allowed_in(db, lead[1], 'requests')
                    and await user_access.allowed_in(db, provider_id, 'offers'))
        if not eligible:
            await cancel_in(db, lead_id, provider_id, actor_id=0, reason='lead_or_recipient_unavailable')
        else:
            await audit_delivery_in(db, lead_id, provider_id, 'provider_notification_attempt', 'pending')
        await db.commit()
        return bool(eligible)


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
        if cursor.rowcount == 1 and await (await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_action_log'"
        )).fetchone():
            await audit_delivery_in(db, lead_id, provider_id, 'provider_notification_queued', 'pending')
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
