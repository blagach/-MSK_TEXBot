"""Opt-in referral attribution in the existing SQLite. No Telegram calls."""
import json
import logging
import re
import secrets
from datetime import datetime, timezone

import aiosqlite
import storage
import user_access

CODE = re.compile(r"r1_([A-Za-z0-9_-]{22})\Z")
_schema_available = True


def now():
    return datetime.now(timezone.utc).isoformat()


async def init_schema(path):
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS referral_links (
                owner_id INTEGER PRIMARY KEY CHECK(owner_id>0),
                code TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS referral_attributions (
                invited_id INTEGER PRIMARY KEY CHECK(invited_id>0),
                inviter_id INTEGER NOT NULL REFERENCES referral_links(owner_id),
                attributed_at TEXT NOT NULL, start_update_id INTEGER UNIQUE,
                first_lead_id INTEGER REFERENCES leads(id), first_lead_at TEXT,
                provider_completed_at TEXT,
                CHECK(invited_id != inviter_id),
                CHECK((first_lead_id IS NULL) = (first_lead_at IS NULL)));
            CREATE INDEX IF NOT EXISTS referral_by_inviter ON referral_attributions(inviter_id);
        ''')
        await db.commit()


async def mode_in(db):
    row = await (await db.execute("SELECT id,result FROM admin_action_log WHERE action='referral_mode' ORDER BY id DESC LIMIT 1")).fetchone()
    enabled = _schema_available and bool(row and row[1] == 'enabled')
    if enabled:
        # A partial/failed optional migration must not expose broken entry points.
        count = await (await db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('referral_links','referral_attributions')")).fetchone()
        enabled = count[0] == 2
    return {'revision': row[0] if row else 0, 'enabled': enabled}


def fault(operation, error):
    logging.error('Referral feature failed: operation=%s error=%s; core operation retained, referral statistics may be incomplete',
                  operation, type(error).__name__)


async def init_optional_schema(path):
    global _schema_available
    try:
        await init_schema(path)
        _schema_available = True
        return True
    except Exception as error:
        # Optional feature boundary, no Telegram requests or core schema work here.
        fault('schema', error)
        _schema_available = False
        return False


async def record_visit_safely(uid, payload=None, update_id=None):
    try:
        return await record_visit(uid, payload, update_id)
    except Exception as error:
        fault('attribution', error)
        # Preserve original visitor tracking; don't later steal an organic/failed first touch.
        await storage.record_visitor(uid)
        return False


async def record_after_commit(operation, uid, lead_id=None, *, path=None):
    """Best-effort metrics AFTER the independent business commit and connection close.

    Catch errors only at this optional SQLite-only boundary, never Telegram or
    business exceptions. No retry, historical replay, or background sender.
    """
    try:
        async with aiosqlite.connect(path or storage.DB_PATH, timeout=0.1) as db:
            # Legacy helper callers may have only the payment-preference schema.
            if not await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_action_log'")).fetchone():
                return False
            if not (await mode_in(db))['enabled']:
                return False
            await db.execute('BEGIN IMMEDIATE')
            if operation == 'first_lead':
                await first_lead(db, uid, lead_id)
            elif operation == 'provider_complete':
                await provider_complete(db, uid)
            else:
                raise ValueError('Unknown referral operation')
            await db.commit()
        return True
    except Exception as error:
        fault(operation, error)
        return False


async def mode():
    async with aiosqlite.connect(storage.DB_PATH) as db:
        return await mode_in(db)


async def set_mode(actor, owner, enabled, revision):
    if owner is None or actor != owner:
        raise PermissionError('Только владелец')
    if type(enabled) is not bool or type(revision) is not int:
        raise ValueError('Invalid referral mode')
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute('BEGIN IMMEDIATE')
        if (await mode_in(db))['revision'] != revision:
            return False
        await db.execute("INSERT INTO admin_action_log(actor_id,action,result,details,created_at) VALUES (?,?,?,?,?)",
                         (actor, 'referral_mode', 'enabled' if enabled else 'paused', json.dumps({'previous_revision': revision}), now()))
        await db.commit()
        return True


async def eligible_in(db, uid):
    if type(uid) is not int or not 0 < uid < 2**63:
        return False
    if not await user_access.allowed_in(db, uid, 'full'):
        return False
    row = await (await db.execute('SELECT blocked FROM provider_profiles WHERE telegram_id=?', (uid,))).fetchone()
    return not row or not row[0]


async def link_code(uid):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute('BEGIN IMMEDIATE')
        if not (await mode_in(db))['enabled'] or not await eligible_in(db, uid):
            return None
        old = await (await db.execute('SELECT code FROM referral_links WHERE owner_id=?', (uid,))).fetchone()
        if old:
            return old[0]
        for _ in range(4):
            code = secrets.token_urlsafe(16)
            try:
                await db.execute('INSERT INTO referral_links VALUES (?,?,?)', (uid, code, now()))
            except aiosqlite.IntegrityError:
                continue
            await db.commit()
            return code
        raise RuntimeError('Could not allocate referral code')


async def known_in(db, uid):
    # Historical business identities are excluded even if bot_visitors is incomplete.
    for table, column in [('bot_visitors', 'telegram_id'), ('provider_profiles', 'telegram_id'),
                          ('providers', 'telegram_id'), ('leads', 'customer_id'), ('offers', 'provider_id'),
                          ('support_tickets', 'requester_id'), ('referral_links', 'owner_id')]:
        if await (await db.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (uid,))).fetchone():
            return True
    # DefaultKeyBuilder encodes bot/chat/user as distinct colon-delimited fields.
    return bool(await (await db.execute('SELECT 1 FROM form_drafts WHERE storage_key LIKE ? LIMIT 1',
                                       (f'fsm:%:{uid}:{uid}:%',))).fetchone())


async def record_visit(uid, payload=None, update_id=None):
    """Run BEFORE record_visitor, atomically. Strict first-touch, including organic starts."""
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute('PRAGMA foreign_keys=ON')
        await db.execute('BEGIN IMMEDIATE')
        attributed = False
        enabled = (await mode_in(db))['enabled']
        match = CODE.fullmatch(payload) if isinstance(payload, str) else None
        if enabled and match and not await known_in(db, uid) and await eligible_in(db, uid):
            row = await (await db.execute('SELECT owner_id FROM referral_links WHERE code=?', (match[1],))).fetchone()
            if row and row[0] != uid and await eligible_in(db, row[0]):
                cur = await db.execute('INSERT INTO referral_attributions(invited_id,inviter_id,attributed_at,start_update_id) '
                                       'VALUES (?,?,?,?) ON CONFLICT DO NOTHING', (uid, row[0], now(), update_id))
                attributed = cur.rowcount == 1
        await db.execute('INSERT INTO bot_visitors VALUES (?) ON CONFLICT DO NOTHING', (uid,))
        await db.commit()
        return attributed


async def first_lead(db, uid, lead_id):
    if (await mode_in(db))['enabled']:
        await db.execute('UPDATE referral_attributions SET first_lead_id=?,first_lead_at=? '
                         'WHERE invited_id=? AND first_lead_id IS NULL', (lead_id, now(), uid))


async def provider_complete(db, uid):
    if not (await mode_in(db))['enabled']:
        return
    profile = await (await db.execute('SELECT consent_at FROM provider_profiles WHERE telegram_id=? AND blocked=0', (uid,))).fetchone()
    payment = await (await db.execute('SELECT 1 FROM provider_payment_preferences WHERE provider_id=? LIMIT 1', (uid,))).fetchone()
    if profile and profile[0] and payment:
        await db.execute('UPDATE referral_attributions SET provider_completed_at=? '
                         'WHERE invited_id=? AND provider_completed_at IS NULL', (now(), uid))


async def stats(uid=None):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        row = await (await db.execute('SELECT count(*),coalesce(sum(first_lead_id IS NOT NULL OR provider_completed_at IS NOT NULL),0),'
                                     'coalesce(sum(first_lead_id IS NOT NULL),0),coalesce(sum(provider_completed_at IS NOT NULL),0) '
                                     'FROM referral_attributions' + (' WHERE inviter_id=?' if uid is not None else ''),
                                     (uid,) if uid is not None else ())).fetchone()
        return dict(zip(('arrived', 'active', 'customers', 'providers'), row))
