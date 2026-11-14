"""Local payment/order experiments; TEST units only, no tariffs or network."""
import hashlib
import hmac
import json
from pathlib import Path
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
        return self.event(order, 'paid', event_id)

    def event(self, order, status, event_id=None):
        payload = {k: order[k] for k in ('id', 'payer', 'amount', 'currency', 'provider_ref')}
        payload.update(event_id=event_id or uuid4().hex, mode='local_sandbox', status=status)
        return {**payload, 'signature': hmac.new(self.key, canonical(payload).encode(), hashlib.sha256).hexdigest()}

    def verify(self, event):
        data = {k: v for k, v in event.items() if k != 'signature'}
        expected = hmac.new(self.key, canonical(data).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(str(event.get('signature', '')), expected):
            raise ValueError('Invalid fake event signature')
        if (data.get('mode') != 'local_sandbox' or data.get('currency') != 'TEST'
                or data.get('status') not in ('paid', 'failed', 'cancelled', 'refunded')):
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
            CREATE TABLE IF NOT EXISTS sandbox_payment_outcomes (
                event_id TEXT PRIMARY KEY REFERENCES sandbox_payment_events(event_id),
                order_id TEXT NOT NULL REFERENCES sandbox_orders(id), status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_payment_refunds (
                order_id TEXT PRIMARY KEY REFERENCES sandbox_orders(id),
                event_id TEXT NOT NULL UNIQUE REFERENCES sandbox_payment_events(event_id)
            );
            CREATE TABLE IF NOT EXISTS sandbox_order_cancellations (
                payer INTEGER NOT NULL, request_key TEXT NOT NULL,
                order_id TEXT NOT NULL REFERENCES sandbox_orders(id),
                PRIMARY KEY(payer,request_key)
            );
            CREATE TABLE IF NOT EXISTS sandbox_right_policies (
                order_id TEXT PRIMARY KEY REFERENCES sandbox_orders(id),
                grant_mode TEXT NOT NULL CHECK(grant_mode IN ('quota','period')),
                duration REAL
            );
        ''')

    def create_order(self, payer, request_key, capability, amount, units=1, expires_at=None, currency='TEST',
                     *, grant_mode='quota', duration=None):
        with self.store.transaction() as db:
            return self.create_order_in(db, payer, request_key, capability, amount, units, expires_at, currency,
                                        grant_mode=grant_mode, duration=duration)

    def create_order_in(self, db, payer, request_key, capability, amount, units=1, expires_at=None, currency='TEST',
                        *, grant_mode='quota', duration=None):
        """Participate in the caller's owned sandbox transaction (checkout)."""
        self.store.validate()
        databases = db.execute('PRAGMA database_list').fetchall()
        if not db.in_transaction or not any(r[1] == 'main' and Path(r[2]).resolve() == self.store.path for r in databases):
            raise ValueError('An owned sandbox transaction is required')
        positive(payer); positive(amount); positive(units)
        if currency != 'TEST' or not request_key or not capability:
            raise ValueError('Only explicit TEST orders with a capability and request key are allowed')
        if expires_at is not None:
            expires_at = instant(expires_at)
        if duration is not None:
            duration = instant(duration)
            if duration <= 0 or expires_at is not None:
                raise ValueError('Use either absolute expiry or a positive grant duration')
        if grant_mode not in ('quota', 'period') or (grant_mode == 'period' and (duration is None or units != 1)):
            raise ValueError('A period grant needs a duration and one nonconsumable unit')
        payload = dict(payer=payer, capability=capability, amount=amount, currency=currency,
                       units=units, expires_at=expires_at)
        if grant_mode != 'quota' or duration is not None:
            payload.update(grant_mode=grant_mode, duration=duration)
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        old = db.execute('SELECT * FROM sandbox_orders WHERE payer=? AND request_key=?',
                         (payer, request_key)).fetchone()
        if old:
            if old['payload_hash'] != digest:
                raise ValueError('Request key reused for a different order')
            return self._order_view(db, old)
        order_id = uuid4().hex
        db.execute('INSERT INTO sandbox_orders VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (order_id, payer, request_key, capability, amount, currency, units, expires_at,
                    'pending', 'fake_' + order_id, digest))
        if grant_mode != 'quota' or duration is not None:
            db.execute('INSERT INTO sandbox_right_policies VALUES (?,?,?)', (order_id, grant_mode, duration))
        return self._order_view(db, db.execute('SELECT * FROM sandbox_orders WHERE id=?', (order_id,)).fetchone())

    def confirm(self, provider, event, *, now=None):
        """Compatibility entrypoint: it accepts only a paid event."""
        return self.apply_event(provider, event, expected_status='paid', now=now)

    def apply_event(self, provider, event, expected_status=None, *, now=None):
        if type(provider) is not FakePaymentProvider:
            raise TypeError('Only the fake provider is permitted')
        data = provider.verify(event)
        if expected_status is not None and data['status'] != expected_status:
            raise ValueError('Unexpected payment event type')
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
            refunded = db.execute('SELECT 1 FROM sandbox_payment_refunds WHERE order_id=?', (order['id'],)).fetchone()
            if refunded and data['status'] == 'refunded':
                return False
            if refunded or order['status'] == 'cancelled':
                raise ValueError('Order needs reconciliation; do not grant access')
            status = data['status']
            if status in ('failed', 'cancelled') and order['status'] != 'pending':
                raise ValueError('A confirmed payment cannot be failed or cancelled')
            if status == 'refunded' and order['status'] != 'paid':
                raise ValueError('Only a confirmed payment can be refunded')
            db.execute('INSERT INTO sandbox_payment_events VALUES (?,?,?)', (data['event_id'], order['id'], digest))
            db.execute('INSERT INTO sandbox_payment_outcomes VALUES (?,?,?)', (data['event_id'], order['id'], status))
            if status == 'failed':
                # A verified failed attempt does not cancel a retryable order.
                return True
            if status == 'cancelled':
                db.execute("UPDATE sandbox_orders SET status='cancelled' WHERE id=?", (order['id'],))
                return True
            if status == 'refunded':
                db.execute('INSERT INTO sandbox_payment_refunds VALUES (?,?)', (order['id'], data['event_id']))
                db.execute('UPDATE sandbox_rights SET remaining=0 WHERE order_id=?', (order['id'],))
                return True
            if order['status'] == 'paid':
                return False
            policy = db.execute('SELECT duration FROM sandbox_right_policies WHERE order_id=?', (order['id'],)).fetchone()
            expiry = order['expires_at']
            if policy and policy[0] is not None:
                expiry = instant(instant(now) + policy[0])
            db.execute('INSERT INTO sandbox_rights VALUES (?,?,?,?,?)',
                       (order['id'], order['payer'], order['capability'], order['units'], expiry))
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
                if db.execute('SELECT 1 FROM sandbox_payment_refunds WHERE order_id=?', (old[0],)).fetchone():
                    return None
                return old[0], False
            grant = db.execute('''SELECT r.order_id,coalesce(p.grant_mode,'quota') AS grant_mode FROM sandbox_rights r
                LEFT JOIN sandbox_right_policies p ON p.order_id=r.order_id
                WHERE r.payer=? AND r.capability=? AND r.remaining>0 AND (r.expires_at IS NULL OR r.expires_at>?)
                ORDER BY r.expires_at IS NULL,r.expires_at,r.order_id LIMIT 1''', (payer, capability, now)).fetchone()
            if not grant:
                return None
            if grant['grant_mode'] == 'quota':
                db.execute('UPDATE sandbox_rights SET remaining=remaining-1 WHERE order_id=?', (grant[0],))
            db.execute('INSERT INTO sandbox_usages VALUES (?,?,?,?)', (payer, capability, request_key, grant[0]))
            return grant[0], True

    def get_order(self, payer, order_id):
        positive(payer)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM sandbox_orders WHERE id=? AND payer=?', (order_id, payer)).fetchone()
            if not row:
                raise ValueError('Unknown owned order')
            return self._order_view(db, row)

    @staticmethod
    def _order_view(db, row):
        # Keep the original paid capture as history; refund is a separate,
        # durable financial event. Every order read uses this effective state.
        result = dict(row)
        policy = db.execute('SELECT grant_mode,duration FROM sandbox_right_policies WHERE order_id=?', (row['id'],)).fetchone()
        if policy:
            right = db.execute('SELECT expires_at FROM sandbox_rights WHERE order_id=?', (row['id'],)).fetchone()
            result.update(grant_mode=policy[0], duration=policy[1], grant_expires_at=right[0] if right else None)
        if db.execute('SELECT 1 FROM sandbox_payment_refunds WHERE order_id=?', (row['id'],)).fetchone():
            result['status'] = 'refunded'
        elif row['status'] == 'pending':
            attempt = db.execute('SELECT status FROM sandbox_payment_outcomes WHERE order_id=? ORDER BY rowid DESC LIMIT 1',
                                 (row['id'],)).fetchone()
            if attempt and attempt[0] == 'failed':
                result['status'] = 'failed'
        return result

    def cancel(self, payer, order_id, request_key):
        positive(payer)
        if not isinstance(request_key, str) or not request_key:
            raise ValueError('Cancellation request identity required')
        with self.store.transaction() as db:
            row = db.execute('SELECT status FROM sandbox_orders WHERE id=? AND payer=?', (order_id, payer)).fetchone()
            if not row:
                raise ValueError('Unknown owned order')
            old = db.execute('SELECT order_id FROM sandbox_order_cancellations WHERE payer=? AND request_key=?',
                             (payer, request_key)).fetchone()
            if old:
                if old[0] != order_id:
                    raise ValueError('Cancellation identity reused for a different order')
                return False
            if row[0] == 'paid':
                raise ValueError('A paid order requires a verified refund, not cancellation')
            db.execute('INSERT INTO sandbox_order_cancellations VALUES (?,?,?)', (payer, request_key, order_id))
            db.execute("UPDATE sandbox_orders SET status='cancelled' WHERE id=?", (order_id,))
            return row[0] != 'cancelled'

    def access_resource(self, payer, capability, resource_id, now, *, authorized):
        """Opaque local resource receipt, never a live contact or a tariff.

        Caller must recheck current role/resource rights each time. An existing
        receipt reopens without another quota debit; refunds fail closed.
        """
        if type(authorized) is not bool or not authorized:
            raise PermissionError('Current resource authorization required')
        if not isinstance(resource_id, str) or not resource_id or len(resource_id) > 200:
            raise ValueError('Stable resource identity required')
        return self.consume(payer, capability, 'resource:' + resource_id, now)
