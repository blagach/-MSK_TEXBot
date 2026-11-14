"""Opt-in feature lab for the exact isolated test Worker; never production.

Enabling MSK_TEST_FEATURE_LAB=1 is a separate owner-approved environment change.
The ordinary launcher/configuration defaults to off. Business SQLite is read
only here; all TEST payment/campaign state lives in a separate marked sidecar.
"""
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import user_access
from sandbox_checkout import BusinessCopy, SandboxCheckout, TestOffer, TestResource

SERVICE = 'srv-dboqc51srm7s73cppi00'
USERNAME = 'Msktex_test_dmitry_bot'
BOT_ID = 8637104640
DISK = Path('/var/data')
FLAG = 'MSK_TEST_FEATURE_LAB'


class VerifiedTestScope:
    def __init__(self, bot, identity, owner, testers, database):
        self.bot, self.owner, self.testers = bot, owner, frozenset(testers)
        self.database = Path(database)
        if (identity.id != BOT_ID or identity.username != USERNAME or not identity.is_bot
                or bot.id != BOT_ID or type(owner) is not int or owner <= 0 or owner not in self.testers
                or any(type(uid) is not int or uid <= 0 for uid in self.testers)):
            raise RuntimeError('Verified isolated test identity and tester owner required')
        self.ensure()

    def ensure(self):
        if (os.getenv('RENDER_SERVICE_ID') != SERVICE or os.getenv(FLAG) != '1'
                or self.database != DISK/'bot.db' or DISK.is_symlink() or not DISK.is_mount()
                or self.database.is_symlink() or not self.database.is_file()):
            raise RuntimeError('Feature lab is permitted only on the explicitly enabled isolated test service')

    def store_path(self, path):
        self.ensure()
        path = Path(path)
        if path != DISK/'feature-lab-test.sqlite' or path.is_symlink():
            raise RuntimeError('Only the separate test feature sidecar may be written')
        return path.resolve()

    def session(self, bot, uid=None, chat=None):
        self.ensure()
        if bot is not self.bot or bot.id != BOT_ID:
            raise RuntimeError('A different bot cannot use this test feature scope')
        if uid is not None and (uid not in self.testers or chat is None or chat.type != 'private' or chat.id != uid):
            raise PermissionError('Feature lab is restricted to the configured private test accounts')


class TestBusinessPolicy(BusinessCopy):
    def __init__(self, scope):
        if type(scope) is not VerifiedTestScope:
            raise RuntimeError('Verified test scope required')
        self.scope, self.path = scope, scope.database
        self.owner, self.legacy_admins = scope.owner, frozenset({scope.owner})
        self.check()

    def check(self):
        self.scope.ensure()


def capture(db):
    schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
    rows = {n: sorted(db.execute('SELECT * FROM "'+n.replace('"','""')+'"').fetchall(), key=repr)
            for (n,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchall():
        raise RuntimeError('Invalid feature sidecar')
    return schema, rows


def backup_existing(scope, path):
    scope.store_path(path)
    root = DISK/'backups'
    if root.is_symlink():
        raise RuntimeError('Unsafe backup directory')
    root.mkdir(mode=0o700, exist_ok=True)
    directory = root/('feature-lab-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid4().hex[:8])
    directory.mkdir(mode=0o700)
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as src:
        src.execute('PRAGMA query_only=ON'); src.execute('BEGIN')
        before = capture(src)
        for name, source in (('snapshot.sqlite', src), ('restore.sqlite', None)):
            target = directory/name
            target.touch(mode=0o600, exist_ok=False)
            with sqlite3.connect(target) as dst:
                if source is not None:
                    source.backup(dst)
                else:
                    with sqlite3.connect((directory/'snapshot.sqlite').as_uri()+'?mode=ro', uri=True) as snapshot:
                        snapshot.backup(dst)
                if capture(dst) != before:
                    raise RuntimeError('Feature sidecar restore mismatch')
        src.rollback()
    report = {'original_tables': len(before[1]), 'files': [
        {'name': n, 'bytes': (directory/n).stat().st_size,
         'sha256': hashlib.sha256((directory/n).read_bytes()).hexdigest()} for n in ('snapshot.sqlite','restore.sqlite')]}
    with (directory/'report.json').open('x') as f:
        json.dump(report, f)
    return before


class TestFeatures:
    def __init__(self, scope):
        from sandbox_store import SandboxStore
        from sandbox_admin_ui import LocalAdmin
        from sandbox_campaigns import FakeTransport
        if type(scope) is not VerifiedTestScope:
            raise RuntimeError('Verified test scope required')
        self.scope = scope
        path = scope.store_path(DISK/'feature-lab-test.sqlite')
        before = backup_existing(scope, path) if path.exists() else None
        self.store = SandboxStore(path, test_scope=scope) if path.exists() else SandboxStore.create(path, test_scope=scope)
        self.business = TestBusinessPolicy(scope)
        self.admin = LocalAdmin(self.store, scope.owner, transport=FakeTransport())
        offers = (TestOffer('demo-one','Учебная функция','sample_feature','function',100),
                  TestOffer('demo-pack','Учебный пакет','sample_feature','package',100,3),
                  TestOffer('demo-time','Учебный срок','sample_period','period',100,1,3600))
        resources = (TestResource('demo-item','sample_feature'), TestResource('demo-time-item','sample_period'))
        # There is no real tariff switch in this release. All existing contacts
        # and this lab's demo resources retain default free access.
        self.checkout = SandboxCheckout(self.store, self.business, offers, resources, enforce_paid=False)
        if before is not None:
            with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
                after = capture(db)
            if not set(before[0]) <= set(after[0]) or any(after[1].get(k) != v for k,v in before[1].items()):
                raise RuntimeError('Feature sidecar initialization changed existing data')
        self.admin.before_action = self.refresh_audience

    async def refresh_audience(self):
        # Read only the approved testers. Preserve their marketing choices; do
        # not equate service registration with consent to a campaign.
        async with self.business.read() as db:
            for uid in self.scope.testers:
                profile = await (await db.execute('SELECT blocked FROM provider_profiles WHERE telegram_id=?', (uid,))).fetchone()
                equipment = await (await db.execute('SELECT region,category FROM provider_equipment WHERE provider_id=? AND active=1',
                                                   (uid,))).fetchall()
                zones = {r[0] for r in equipment}
                if 'Москва и Московская область' in zones:
                    zones = {'Москва','Московская область'}
                zones &= {'Москва','Московская область'}
                restricted = (await user_access.snapshot_in(db, uid))['scopes']
                blocked = not profile or bool(profile[0]) or 'full' in restricted or not zones
                with self.store.connection() as sidecar:
                    old = sidecar.execute('SELECT * FROM sandbox_recipients WHERE id=?', (uid,)).fetchone()
                if zones:
                    self.admin.campaigns.recipient(uid, 'provider', zones, {r[1] for r in equipment}, blocked=blocked)
                elif old:
                    self.admin.campaigns.recipient(uid, old['role'], json.loads(old['regions']),
                                                  json.loads(old['categories']), blocked=True)

    def install(self, dispatcher):
        from sandbox_local_telegram import local_dispatcher
        async def preserve_active_form(handler, event, data):
            text = (getattr(event, 'text', None) or '').split(maxsplit=1)[0:1]
            command = text[0].split('@')[0] if text else ''
            state = data.get('state')
            module = getattr(getattr(data.get('handler'), 'callback', None), '__module__', '')
            if module not in ('sandbox_local_telegram','sandbox_checkout_ui') and event.from_user:
                uid = event.from_user.id
                with self.store.transaction() as db:
                    db.execute('UPDATE sandbox_admin_sessions SET nonce=?,prompt_id=NULL WHERE actor=? AND busy=0',
                               (uuid4().hex[:12], uid))
                    db.execute('UPDATE sandbox_checkout_ui SET nonce=? WHERE payer=? AND busy=0', (uuid4().hex[:12], uid))
                    db.execute('UPDATE sandbox_consent_panels SET nonce=? WHERE recipient=?', (uuid4().hex[:12], uid))
            if command in ('/lab', '/checkout', '/consent') and state is not None and await state.get_state():
                from bot import pause_form
                await pause_form(event, state)
            return await handler(event, data)
        dispatcher.message.middleware(preserve_active_form)
        dispatcher.callback_query.middleware(preserve_active_form)
        local_dispatcher(self.admin, time.time, checkout=self.checkout, business=self.business,
                         scheduler_interval=1.1, dispatcher=dispatcher, admin_command='lab', test_scope=self.scope)
