"""TEST checkout and resource rights using the existing domain policy, read-only.

Business data must be a separately restored local copy. Payments and UI state
live in an owned sandbox sidecar. Free customer_contacts is never wrapped here.
"""
import hashlib
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import aiosqlite
import admin_access
import user_access
from sandbox_payments import FakePaymentProvider, SandboxPayments, canonical, positive
from sandbox_store import SandboxStore, instant

COPY_MARKER = 'MSK_LOCAL_RESTORED_BUSINESS_V1'


class BusinessCopy:
    @classmethod
    def restore(cls, snapshot, destination, *, owner, legacy_admins=()):
        source, target = SandboxStore.local_path(snapshot), SandboxStore.local_path(destination)
        if source == target or not source.is_file():
            raise ValueError('A separate existing local snapshot is required')
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(source.as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
            src.execute('PRAGMA query_only=ON'); src.execute('BEGIN')
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or dst.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Invalid restored business copy')
            schema = src.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            if schema != dst.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall():
                raise ValueError('Restored schema differs')
            for (name,) in src.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                query = 'SELECT * FROM "'+name.replace('"','""')+'"'
                if sorted(src.execute(query).fetchall(), key=repr) != sorted(dst.execute(query).fetchall(), key=repr):
                    raise ValueError('Restored rows differ')
            src.rollback()
        proof = {'marker': COPY_MARKER, 'path': str(target), 'inode': target.stat().st_ino,
                 'initial_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
        with target.with_name(target.name+'.copy-proof.json').open('x') as f:
            json.dump(proof, f)
        return cls(target, owner=owner, legacy_admins=legacy_admins)

    def __init__(self, path, *, owner, legacy_admins=()):
        self.path = SandboxStore.local_path(path)
        self.owner, self.legacy_admins = positive(owner), frozenset(legacy_admins)
        self.check()

    def check(self):
        SandboxStore.local_path(self.path)
        proof = json.loads(self.path.with_name(self.path.name+'.copy-proof.json').read_text())
        if proof.get('marker') != COPY_MARKER or proof.get('path') != str(self.path) or proof.get('inode') != self.path.stat().st_ino:
            raise ValueError('An explicitly restored owned local business copy is required')

    @asynccontextmanager
    async def read(self):
        self.check()
        async with aiosqlite.connect(self.path.as_uri()+'?mode=ro', uri=True) as db:
            await db.execute('PRAGMA query_only=ON')
            await db.execute('BEGIN')
            try:
                yield db
            finally:
                await db.rollback()

    async def staff(self, actor):
        positive(actor)
        async with self.read() as db:
            role = await admin_access.role_in(db, actor, self.legacy_admins, self.owner)
            return role, (admin_access.ALL_PERMISSIONS if role == 'owner' else admin_access.PERMISSIONS.get(role, ()))

    @asynccontextmanager
    async def resource_scope(self, actor, role, scope):
        """Reuse actual restriction and profile rules within a read snapshot."""
        positive(actor)
        async with self.read() as db:
            if not await user_access.allowed_in(db, actor, scope):
                raise PermissionError('Доступ ограничен текущими правилами сервиса.')
            if role == 'provider':
                row = await (await db.execute('SELECT blocked FROM provider_profiles WHERE telegram_id=?', (actor,))).fetchone()
                allowed = row is not None and not row[0]
            elif role == 'customer':
                allowed = bool(await (await db.execute('SELECT 1 FROM leads WHERE customer_id=? LIMIT 1', (actor,))).fetchone())
            elif role == 'visitor':
                allowed = bool(await (await db.execute('SELECT 1 FROM bot_visitors WHERE telegram_id=?', (actor,))).fetchone())
            else:
                allowed = False
            if not allowed:
                raise PermissionError('Текущая роль не разрешает этот ресурс.')
            yield


@dataclass(frozen=True)
class TestOffer:
    id: str
    title: str
    capability: str
    model: str
    amount: int
    units: int = 1
    duration: float | None = None
    role: str = 'provider'
    scope: str = 'offers'

    def terms(self):
        if (not self.id or not self.title or not self.capability.startswith(('sample_', 'demo.'))
                or self.model not in ('function', 'package', 'period')
                or self.role not in ('provider', 'customer', 'visitor') or self.scope not in user_access.SCOPES):
            raise ValueError('Use an explicit local offer, never a live contact tariff')
        positive(self.amount); positive(self.units)
        if self.model == 'function' and self.units != 1:
            raise ValueError('A single-function grant uses one unit')
        if self.model == 'period' and (self.duration is None or self.units != 1):
            raise ValueError('A period grant needs a duration')
        if self.duration is not None and instant(self.duration) <= 0:
            raise ValueError('Invalid grant duration')
        return {**asdict(self), 'currency': 'TEST'}


@dataclass(frozen=True)
class TestResource:
    id: str
    capability: str
    role: str = 'provider'
    scope: str = 'offers'


class SandboxCheckout:
    def __init__(self, store, business, offers, resources, *, enforce_paid=False):
        if type(enforce_paid) is not bool or not isinstance(business, BusinessCopy):
            raise ValueError('Explicit local business policy required')
        self.store, self.business = store, business
        self.payments = SandboxPayments(store)
        self.offers = MappingProxyType({o.id: o for o in offers})
        self.resources = MappingProxyType({r.id: r for r in resources})
        self.enforce_paid = enforce_paid  # Default free; never read from live env.
        for offer in self.offers.values():
            offer.terms()
        for resource in self.resources.values():
            if (not resource.id or not resource.capability.startswith(('sample_', 'demo.'))
                    or resource.role not in ('provider', 'customer', 'visitor') or resource.scope not in user_access.SCOPES):
                raise ValueError('Only synthetic protected resources are permitted')
        store.schema('''CREATE TABLE IF NOT EXISTS sandbox_checkouts (
            id TEXT PRIMARY KEY, payer INTEGER NOT NULL, request_key TEXT NOT NULL,
            offer_id TEXT NOT NULL, terms TEXT NOT NULL, token TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('review','submitted','cancelled')),
            order_id TEXT REFERENCES sandbox_orders(id), UNIQUE(payer,request_key)
        );''')

    async def begin(self, payer, offer_id, request_key):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 100:
            raise ValueError('Checkout identity required')
        offer = self.offers[offer_id]
        terms = canonical(offer.terms())
        async with self.business.resource_scope(payer, offer.role, offer.scope):
            with self.store.transaction() as db:
                row = db.execute('SELECT * FROM sandbox_checkouts WHERE payer=? AND request_key=?',
                                 (payer, request_key)).fetchone()
                if row:
                    if row['terms'] != terms:
                        raise ValueError('Checkout identity reused with different terms')
                    return dict(row)
                cid = uuid4().hex
                db.execute('INSERT INTO sandbox_checkouts VALUES (?,?,?,?,?,?,\'review\',NULL)',
                           (cid, payer, request_key, offer.id, terms, uuid4().hex))
                return dict(db.execute('SELECT * FROM sandbox_checkouts WHERE id=?', (cid,)).fetchone())

    def owned(self, payer, checkout_id):
        positive(payer)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM sandbox_checkouts WHERE id=? AND payer=?', (checkout_id, payer)).fetchone()
            if not row:
                raise PermissionError('Нет доступа к этому оформлению.')
            return dict(row)

    async def submit(self, payer, checkout_id, token):
        row = self.owned(payer, checkout_id)
        offer = self.offers[row['offer_id']]
        async with self.business.resource_scope(payer, offer.role, offer.scope):
            with self.store.transaction() as db:
                row = db.execute('SELECT * FROM sandbox_checkouts WHERE id=? AND payer=?', (checkout_id, payer)).fetchone()
                if row['token'] != token or row['terms'] != canonical(offer.terms()) or row['state'] == 'cancelled':
                    raise ValueError('Оформление изменилось; подтвердите актуальные условия.')
                if row['state'] == 'submitted':
                    return self.payments._order_view(db, db.execute('SELECT * FROM sandbox_orders WHERE id=?', (row['order_id'],)).fetchone())
                # Order, policy and submitted state commit together. No external
                # provider creation, intermediate unknown order or second debit.
                order = self.payments.create_order_in(db, payer, 'checkout:'+checkout_id, offer.capability,
                            offer.amount, offer.units, grant_mode='period' if offer.model == 'period' else 'quota',
                            duration=offer.duration)
                db.execute("UPDATE sandbox_checkouts SET state='submitted',order_id=? WHERE id=?", (order['id'], checkout_id))
                return order

    async def simulate(self, payer, checkout_id, status, now):
        row = self.owned(payer, checkout_id)
        if row['state'] != 'submitted':
            raise ValueError('Сначала подтвердите оформление.')
        terms = json.loads(row['terms'])
        async with self.business.resource_scope(payer, terms['role'], terms['scope']):
            order = self.payments.get_order(payer, row['order_id'])
            provider = FakePaymentProvider()
            event = provider.event(order, status, f'checkout:{checkout_id}:{status}')
            return self.payments.apply_event(provider, event, now=now)

    async def cancel(self, payer, checkout_id):
        row = self.owned(payer, checkout_id)
        if row['state'] == 'submitted':
            self.payments.cancel(payer, row['order_id'], 'checkout:'+checkout_id)
        with self.store.transaction() as db:
            # Recheck atomically: a concurrent submit cannot be hidden by cancel.
            current = db.execute('SELECT * FROM sandbox_checkouts WHERE id=?', (checkout_id,)).fetchone()
            if current['state'] == 'submitted' and current['order_id'] != row['order_id']:
                raise ValueError('Заказ появился одновременно; проверьте его статус.')
            db.execute("UPDATE sandbox_checkouts SET state='cancelled' WHERE id=?", (checkout_id,))

    async def open_resource(self, payer, resource_id, now):
        resource = self.resources[resource_id]  # Capability/role never supplied by caller.
        async with self.business.resource_scope(payer, resource.role, resource.scope):
            if not self.enforce_paid:
                return ('free', False)
            return self.payments.access_resource(payer, resource.capability, resource.id, now, authorized=True)
