"""Local payment/order experiments; TEST units only, no tariffs or network."""
import hashlib
import hmac
import json
from uuid import uuid4

from sandbox_store import instant


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Expected a positive integer')
    return value


class FakePaymentProvider:
    # Public test key, intentionally not a production credential.
    key = b'MSK-TEXBot-local-fake-provider-only'

    def paid_event(self, order, event_id=None):
        payload = {k: order[k] for k in ('id', 'payer', 'amount', 'currency', 'provider_ref')}
        payload.update(event_id=event_id or uuid4().hex, mode='local_sandbox', status='paid')
        return {**payload, 'signature': hmac.new(self.key, canonical(payload).encode(), hashlib.sha256).hexdigest()}

    def verify(self, event):
        data = {k: v for k, v in event.items() if k != 'signature'}
        expected = hmac.new(self.key, canonical(data).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(event.get('signature', '')), expected):
            raise ValueError('Invalid fake event signature')
        if data.get('mode') != 'local_sandbox' or data.get('currency') != 'TEST' or data.get('status') != 'paid':
            raise ValueError('Only local TEST confirmations are supported')
        if not isinstance(data.get('event_id'), str) or not data['event_id']:
            raise ValueError('Missing event ID')
        positive(data.get('payer')); positive(data.get('amount'))
        if any(not isinstance(data.get(k), str) or not data[k] for k in ('id', 'provider_ref')):
            raise ValueError('Invalid event identity')
        return data


class SandboxPayments:
    def __init__(self, store):
        self.store = store
        store.schema('''
            CREATE TABLE IF NOT EXISTS sandbox_orders (
                id TEXT PRIMARY KEY, payer INTEGER NOT NULL, request_key TEXT NOT NULL,
                capability TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>0),
                currency TEXT NOT NULL CHECK(currency='TEST'), units INTEGER NOT NULL CHECK(units>0),
                expires_at REAL, status TEXT NOT NULL CHECK(status IN ('pending','paid','cancelled')),
                provider_ref TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
                UNIQUE(payer,request_key)
            );
            CREATE TABLE IF NOT EXISTS sandbox_payment_events (
                event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES sandbox_orders(id),
                payload_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_rights (
                order_id TEXT PRIMARY KEY REFERENCES sandbox_orders(id), payer INTEGER NOT NULL,
                capability TEXT NOT NULL, remaining INTEGER NOT NULL CHECK(remaining>=0), expires_at REAL
            );
            CREATE TABLE IF NOT EXISTS sandbox_usages (
                payer INTEGER NOT NULL, capability TEXT NOT NULL, request_key TEXT NOT NULL,
                order_id TEXT NOT NULL REFERENCES sandbox_rights(order_id),
                PRIMARY KEY(payer,capability,request_key)
            );
        ''')

    def create_order(self, payer, request_key, capability, amount, units=1, expires_at=None, currency='TEST'):
        positive(payer); positive(amount); positive(units)
        if currency != 'TEST' or not request_key or not capability:
            raise ValueError('Only explicit TEST orders with a capability and request key are allowed')
        if expires_at is not None:
            expires_at = instant(expires_at)
        payload = dict(payer=payer, capability=capability, amount=amount, currency=currency,
                       units=units, expires_at=expires_at)
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM sandbox_orders WHERE payer=? AND request_key=?',
                             (payer, request_key)).fetchone()
            if old:
                if old['payload_hash'] != digest:
                    raise ValueError('Request key reused for a different order')
                return dict(old)
            order_id = uuid4().hex
            db.execute('INSERT INTO sandbox_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (order_id, payer, request_key, capability, amount, currency, units, expires_at,
                        'pending', 'fake_' + order_id, digest))
            return dict(db.execute('SELECT * FROM sandbox_orders WHERE id=?', (order_id,)).fetchone())

    def confirm(self, provider, event):
        if type(provider) is not FakePaymentProvider:
            raise TypeError('Only the fake provider is permitted')
        data = provider.verify(event)
        digest = hashlib.sha256(canonical(data).encode()).hexdigest()
        with self.store.transaction() as db:
            order = db.execute('SELECT * FROM sandbox_orders WHERE id=?', (data.get('id'),)).fetchone()
            if not order or any(order[k] != data.get(k) for k in ('payer', 'amount', 'currency', 'provider_ref')):
                raise ValueError('Confirmation does not match the stored order')
            old = db.execute('SELECT payload_hash FROM sandbox_payment_events WHERE event_id=?',
                             (data['event_id'],)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError('Event ID reused with different content')
                return False
            if order['status'] not in ('pending', 'paid'):
                raise ValueError('Order needs reconciliation; do not grant access')
            db.execute('INSERT INTO sandbox_payment_events VALUES (?,?,?)', (data['event_id'], order['id'], digest))
            if order['status'] == 'paid':
                return False
            db.execute('INSERT INTO sandbox_rights VALUES (?,?,?,?,?)',
                       (order['id'], order['payer'], order['capability'], order['units'], order['expires_at']))
            db.execute("UPDATE sandbox_orders SET status='paid' WHERE id=?", (order['id'],))
            return True

    def consume(self, payer, capability, request_key, now):
        """Return (grant ID, newly consumed); a receipt is not a live app permission."""
        positive(payer)
        instant(now)
        if not request_key or not capability:
            raise ValueError('Missing operation identity')
        with self.store.transaction() as db:
            old = db.execute('SELECT order_id FROM sandbox_usages WHERE payer=? AND capability=? AND request_key=?',
                             (payer, capability, request_key)).fetchone()
            if old:
                return old[0], False
            grant = db.execute('''SELECT order_id FROM sandbox_rights
                WHERE payer=? AND capability=? AND remaining>0 AND (expires_at IS NULL OR expires_at>?)
                ORDER BY expires_at IS NULL,expires_at,order_id LIMIT 1''', (payer, capability, now)).fetchone()
            if not grant:
                return None
            db.execute('UPDATE sandbox_rights SET remaining=remaining-1 WHERE order_id=?', (grant[0],))
            db.execute('INSERT INTO sandbox_usages VALUES (?,?,?,?)', (payer, capability, request_key, grant[0]))
            return grant[0], True
