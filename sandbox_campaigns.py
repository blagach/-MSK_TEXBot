"""Local marketing experiment. Only FakeTransport can consume its queue."""
import hashlib
import json
import time
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sandbox_payments import canonical, positive
from sandbox_store import SandboxBusy, instant


class FakeTransportFailure(RuntimeError):
    pass


def moscow_instant(text):
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo('Europe/Moscow'))
    return value.timestamp()


def regions(values):
    allowed = {'Москва', 'Московская область', 'Москва и Московская область'}
    if not values or not set(values) <= allowed:
        raise ValueError('Explicit Moscow/MO selection required')
    return sorted({'Москва', 'Московская область'} if 'Москва и Московская область' in values else set(values))


class FakeTransport:
    def __init__(self, outcomes=None):
        self.outcomes = {key: list(value) for key, value in (outcomes or {}).items()}
        self.calls, self.possibly_delivered = [], []

    def send(self, recipient, payload):
        self.calls.append((recipient, payload))
        script = self.outcomes.get(recipient, [])
        result = script.pop(0) if script else 'sent'
        if result in ('sent', 'uncertain'):
            self.possibly_delivered.append(recipient)
        if isinstance(result, dict) and result.get('status') == 'rejected_429':
            instant(result.get('retry_after'))
            if result['retry_after'] <= 0 or set(result) != {'status', 'retry_after'}:
                raise ValueError('Invalid explicit fake retry delay')
            return result
        if result not in ('sent', 'uncertain', 'rejected_429', 'failed'):
            if result == 'timeout':
                raise TimeoutError('Fake transport timeout; result unknown')
            raise FakeTransportFailure('Unexpected fake transport failure')
        return result


class SandboxCampaigns:
    def __init__(self, store, owner, marketers=()):
        self.store, self.owner = store, positive(owner)
        self.editors = frozenset(marketers) | {owner}
        store.schema('''
            CREATE TABLE IF NOT EXISTS sandbox_recipients (
                id INTEGER PRIMARY KEY, role TEXT NOT NULL, regions TEXT NOT NULL,
                categories TEXT NOT NULL, blocked INTEGER NOT NULL DEFAULT 0,
                consent INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sandbox_consents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, recipient INTEGER NOT NULL,
                allowed INTEGER NOT NULL, at REAL NOT NULL, purpose TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_campaigns (
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL,
                run_at REAL, approved_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS sandbox_campaign_revisions (
                campaign TEXT NOT NULL REFERENCES sandbox_campaigns(id), revision INTEGER NOT NULL,
                payload TEXT NOT NULL, segment TEXT NOT NULL, actor INTEGER NOT NULL,
                PRIMARY KEY(campaign,revision)
            );
            CREATE TABLE IF NOT EXISTS sandbox_campaign_queue (
                campaign TEXT NOT NULL REFERENCES sandbox_campaigns(id), revision INTEGER NOT NULL,
                recipient INTEGER NOT NULL REFERENCES sandbox_recipients(id), status TEXT NOT NULL,
                retries INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(campaign,revision,recipient)
            );
            CREATE TABLE IF NOT EXISTS sandbox_campaign_approvals (
                campaign TEXT NOT NULL REFERENCES sandbox_campaigns(id), revision INTEGER NOT NULL,
                actor INTEGER NOT NULL, approved_at REAL NOT NULL, run_at REAL NOT NULL, preview_hash TEXT NOT NULL,
                PRIMARY KEY(campaign,revision)
            );
            CREATE TABLE IF NOT EXISTS sandbox_campaign_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT NOT NULL, revision INTEGER NOT NULL,
                recipient INTEGER NOT NULL, outcome TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_campaign_runtime (id INTEGER PRIMARY KEY, next_send REAL NOT NULL);
            INSERT OR IGNORE INTO sandbox_campaign_runtime VALUES (1,0);
            CREATE TABLE IF NOT EXISTS sandbox_campaign_controls (
                id INTEGER PRIMARY KEY AUTOINCREMENT, campaign TEXT NOT NULL REFERENCES sandbox_campaigns(id),
                revision INTEGER NOT NULL, actor INTEGER NOT NULL, action TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sandbox_campaign_quarantine (
                campaign TEXT NOT NULL, revision INTEGER NOT NULL, recipient INTEGER NOT NULL,
                reason TEXT NOT NULL, recorded_at REAL NOT NULL,
                PRIMARY KEY(campaign,revision,recipient)
            );
        ''')

    def editor(self, actor):
        if actor not in self.editors:
            raise PermissionError('Campaign editor permission required')

    def recipient(self, recipient, role, zones, categories=(), blocked=False):
        positive(recipient)
        if role not in ('customer', 'provider', 'both') or type(blocked) is not bool:
            raise ValueError('Invalid recipient')
        with self.store.transaction() as db:
            db.execute('''INSERT INTO sandbox_recipients(id,role,regions,categories,blocked) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET role=excluded.role,regions=excluded.regions,
                categories=excluded.categories,blocked=excluded.blocked''',
                       (recipient, role, canonical(regions(zones)), canonical(sorted(set(categories))), int(blocked)))

    def consent(self, recipient, allowed, now):
        instant(now)
        if type(allowed) is not bool:
            raise ValueError('Consent must be explicit')
        with self.store.transaction() as db:
            self.record_consent(db, recipient, allowed, now)

    @staticmethod
    def record_consent(db, recipient, allowed, now):
        instant(now)
        if type(allowed) is not bool:
            raise ValueError('Consent must be explicit')
        if not db.execute('SELECT 1 FROM sandbox_recipients WHERE id=?', (recipient,)).fetchone():
            raise ValueError('Unknown recipient')
        db.execute('UPDATE sandbox_recipients SET consent=? WHERE id=?', (int(allowed), recipient))
        db.execute('INSERT INTO sandbox_consents(recipient,allowed,at,purpose) VALUES (?,?,?,?)',
                   (recipient, int(allowed), now, 'marketing'))

    @staticmethod
    def content(text, attachments):
        text = text or ''
        attachments = attachments or []
        if not text.strip() and not attachments:
            raise ValueError('Text or attachment required')
        if len(text) > (1024 if attachments else 4096) or len(attachments) > 10:
            raise ValueError('Content exceeds the local transport contract')
        for item in attachments:
            if set(item) != {'kind', 'token'} or item['kind'] not in ('photo', 'video', 'document') or not item['token']:
                raise ValueError('Invalid attachment')
        if len(attachments) > 1 and any(item['kind'] != 'photo' for item in attachments):
            raise ValueError('This prototype supports photo albums and single video/document attachments')
        return canonical({'text': text, 'attachments': attachments})

    def draft(self, actor, text, roles, zones, categories=(), attachments=None):
        self.editor(actor)
        if not roles or not set(roles) <= {'customer', 'provider'}:
            raise ValueError('Explicit role segment required')
        payload = self.content(text, attachments)
        segment = canonical({'roles': sorted(set(roles)), 'regions': regions(zones), 'categories': sorted(set(categories))})
        campaign = uuid4().hex
        with self.store.transaction() as db:
            db.execute("INSERT INTO sandbox_campaigns VALUES (?,1,'draft',NULL,NULL)", (campaign,))
            db.execute('INSERT INTO sandbox_campaign_revisions VALUES (?,1,?,?,?)', (campaign, payload, segment, actor))
        return campaign

    def edit(self, actor, campaign, revision, text, attachments=None):
        self.editor(actor)
        payload = self.content(text, attachments)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM sandbox_campaigns WHERE id=?', (campaign,)).fetchone()
            if not row or row['revision'] != revision or row['state'] not in ('draft', 'approved'):
                raise ValueError('Campaign changed or has already started')
            old = db.execute('SELECT segment FROM sandbox_campaign_revisions WHERE campaign=? AND revision=?',
                             (campaign, revision)).fetchone()
            db.execute("UPDATE sandbox_campaign_queue SET status='superseded' WHERE campaign=? AND status='pending'", (campaign,))
            db.execute('INSERT INTO sandbox_campaign_revisions VALUES (?,?,?,?,?)',
                       (campaign, revision + 1, payload, old[0], actor))
            db.execute("UPDATE sandbox_campaigns SET revision=?,state='draft',run_at=NULL,approved_hash=NULL WHERE id=?",
                       (revision + 1, campaign))
            return revision + 1

    @staticmethod
    def eligible(row, segment):
        return (row['consent'] and not row['blocked']
                and (row['role'] == 'both' or row['role'] in segment['roles'])
                and bool(set(json.loads(row['regions'])) & set(segment['regions']))
                and (not segment['categories'] or bool(set(json.loads(row['categories'])) & set(segment['categories']))))

    @staticmethod
    def targets(db, segment):
        selected = []
        for row in db.execute('SELECT * FROM sandbox_recipients WHERE consent=1 AND blocked=0 ORDER BY id'):
            if SandboxCampaigns.eligible(row, segment):
                selected.append(row['id'])
        return selected

    def view(self, db, campaign, run_at):
        run_at = instant(run_at)
        row = db.execute('''SELECT c.*,r.payload,r.segment FROM sandbox_campaigns c
            JOIN sandbox_campaign_revisions r ON r.campaign=c.id AND r.revision=c.revision WHERE c.id=?''',
                         (campaign,)).fetchone()
        if not row:
            raise ValueError('Unknown campaign')
        body = dict(campaign=campaign, revision=row['revision'], payload=json.loads(row['payload']),
                    segment=json.loads(row['segment']), recipients=self.targets(db, json.loads(row['segment'])), run_at=run_at)
        return body, hashlib.sha256(canonical(body).encode()).hexdigest()

    def preview(self, actor, campaign, run_at):
        self.editor(actor)
        with self.store.connection() as db:
            view, token = self.view(db, campaign, run_at)
            return {**view, 'approval_token': token}

    def approve(self, actor, campaign, revision, token, run_at):
        if actor != self.owner:
            raise PermissionError('Owner approval required')
        run_at = instant(run_at)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM sandbox_campaigns WHERE id=?', (campaign,)).fetchone()
            if not row or row['revision'] != revision:
                raise ValueError('Stale campaign revision')
            if row['state'] == 'approved' and row['approved_hash'] == token:
                if row['run_at'] != run_at:
                    raise ValueError('Schedule changed; confirmation required again')
                return False
            view, expected = self.view(db, campaign, run_at)
            if row['state'] != 'draft' or token != expected:
                raise ValueError('Preview or audience changed; confirmation required again')
            if not view['recipients']:
                raise ValueError('Empty recipient snapshot cannot be approved')
            for recipient in view['recipients']:
                db.execute("INSERT INTO sandbox_campaign_queue(campaign,revision,recipient,status) VALUES (?,?,?,'pending')",
                           (campaign, revision, recipient))
            db.execute("UPDATE sandbox_campaigns SET state='approved',run_at=?,approved_hash=? WHERE id=?",
                       (run_at, token, campaign))
            db.execute('INSERT INTO sandbox_campaign_approvals VALUES (?,?,?,?,?,?)',
                       (campaign, revision, actor, time.time(), run_at, token))
            return True

    @staticmethod
    def event(db, row, outcome, now):
        db.execute('INSERT INTO sandbox_campaign_events(campaign,revision,recipient,outcome,at) VALUES (?,?,?,?,?)',
                   (row['campaign'], row['revision'], row['recipient'], outcome, now))

    @staticmethod
    def finish(db, campaign, revision):
        pending = db.execute("SELECT 1 FROM sandbox_campaign_queue WHERE campaign=? AND revision=? AND status IN ('pending','reserved')",
                             (campaign, revision)).fetchone()
        if not pending:
            db.execute("UPDATE sandbox_campaigns SET state='completed' WHERE id=? AND revision=?", (campaign, revision))

    def recover_reserved(self, now):
        """Explicit local recovery: ambiguous reservations are never requeued."""
        with self.store.exclusive('campaign'):
            return self._recover_reserved(now)

    def _recover_reserved(self, now):
        self.store.require_lock('campaign')
        instant(now)
        with self.store.transaction() as db:
            rows = db.execute("SELECT * FROM sandbox_campaign_queue WHERE status='reserved'").fetchall()
            for row in rows:
                db.execute("UPDATE sandbox_campaign_queue SET status='uncertain' WHERE campaign=? AND revision=? AND recipient=?",
                           (row['campaign'], row['revision'], row['recipient']))
                self.event(db, row, 'uncertain_after_restart', now)
                self.quarantine(db, row, 'uncertain_after_restart', now)
                self.finish(db, row['campaign'], row['revision'])
            return len(rows)

    def pause(self, actor, campaign, revision, now):
        """Stop new claims; already reserved fake I/O may still finish."""
        return self._control(actor, campaign, revision, now, resume=False)

    def resume(self, actor, campaign, revision, now):
        return self._control(actor, campaign, revision, now, resume=True)

    def _control(self, actor, campaign, revision, now, *, resume):
        if actor != self.owner:
            raise PermissionError('Owner control required')
        instant(now)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM sandbox_campaigns WHERE id=?', (campaign,)).fetchone()
            if not row or row['revision'] != revision:
                raise ValueError('Unknown campaign or stale revision')
            if resume:
                if row['state'] in ('approved', 'running'):
                    return False
                if row['state'] != 'paused':
                    raise ValueError('Only an approved paused campaign can resume')
                state, action = 'approved', 'resume'
            else:
                if row['state'] == 'paused':
                    return False
                if row['state'] not in ('approved', 'running'):
                    raise ValueError('Only an approved active campaign can pause')
                state, action = 'paused', 'pause'
            db.execute('UPDATE sandbox_campaigns SET state=? WHERE id=?', (state, campaign))
            db.execute('INSERT INTO sandbox_campaign_controls(campaign,revision,actor,action,at) VALUES (?,?,?,?,?)',
                       (campaign, revision, actor, action, now))
            return True

    def deliver_next(self, transport, now, *, campaign=None):
        try:
            with self.store.exclusive('campaign'):
                return self._deliver_next(transport, now, campaign=campaign)
        except SandboxBusy:
            return None

    def _deliver_next(self, transport, now, *, campaign=None):
        self.store.require_lock('campaign')
        if type(transport) is not FakeTransport:
            raise TypeError('Only the local fake transport is permitted')
        instant(now)
        with self.store.transaction() as db:
            if db.execute('SELECT next_send FROM sandbox_campaign_runtime WHERE id=1').fetchone()[0] > now:
                return None
            row = db.execute('''SELECT q.*,r.payload,r.segment FROM sandbox_campaign_queue q
                JOIN sandbox_campaigns c ON c.id=q.campaign AND c.revision=q.revision
                JOIN sandbox_campaign_revisions r ON r.campaign=q.campaign AND r.revision=q.revision
                WHERE q.status='pending' AND c.state IN ('approved','running') AND c.run_at<=?
                AND (? IS NULL OR c.id=?)
                ORDER BY c.run_at,c.id,q.recipient LIMIT 1''', (now, campaign, campaign)).fetchone()
            if not row:
                return None
            recipient = db.execute('SELECT * FROM sandbox_recipients WHERE id=?', (row['recipient'],)).fetchone()
            if not self.eligible(recipient, json.loads(row['segment'])):
                db.execute("UPDATE sandbox_campaign_queue SET status='excluded' WHERE campaign=? AND revision=? AND recipient=?",
                           (row['campaign'], row['revision'], row['recipient']))
                self.event(db, row, 'excluded', now)
                self.finish(db, row['campaign'], row['revision'])
                return 'excluded'
            db.execute("UPDATE sandbox_campaign_queue SET status='reserved' WHERE campaign=? AND revision=? AND recipient=?",
                       (row['campaign'], row['revision'], row['recipient']))
            db.execute("UPDATE sandbox_campaigns SET state='running' WHERE id=?", (row['campaign'],))
            db.execute('UPDATE sandbox_campaign_runtime SET next_send=? WHERE id=1', (now + 1.05,))
        # Reservation is durable before the fake I/O; no SQLite transaction is
        # held while a transport is called. Unexpected failures leave it reserved.
        outcome = transport.send(row['recipient'], json.loads(row['payload']))
        retry_after = 5
        if isinstance(outcome, dict):
            retry_after, outcome = outcome['retry_after'], outcome['status']
        with self.store.transaction() as db:
            current = db.execute('SELECT status FROM sandbox_campaign_queue WHERE campaign=? AND revision=? AND recipient=?',
                                 (row['campaign'], row['revision'], row['recipient'])).fetchone()[0]
            if current != 'reserved':
                # Explicit recovery may have marked the in-flight call unknown.
                # A late return must not resurrect or requeue that reservation.
                self.event(db, row, 'late_result_' + outcome, now)
                return current
            if outcome == 'rejected_429' and row['retries'] == 0:
                status = 'pending'
                db.execute('UPDATE sandbox_campaign_runtime SET next_send=max(next_send,?) WHERE id=1', (now + retry_after,))
            else:
                status = 'failed' if outcome == 'rejected_429' else outcome
            db.execute('UPDATE sandbox_campaign_queue SET status=?,retries=retries+? WHERE campaign=? AND revision=? AND recipient=?',
                       (status, int(outcome == 'rejected_429'), row['campaign'], row['revision'], row['recipient']))
            self.event(db, row, outcome, now)
            if status == 'uncertain':
                self.quarantine(db, row, 'transport_uncertain', now)
            self.finish(db, row['campaign'], row['revision'])
            return status

    @staticmethod
    def quarantine(db, row, reason, now):
        db.execute('INSERT OR IGNORE INTO sandbox_campaign_quarantine VALUES (?,?,?,?,?)',
                   (row['campaign'], row['revision'], row['recipient'], reason, now))

    def report(self, actor, campaign):
        self.editor(actor)
        with self.store.connection() as db:
            return [dict(r) for r in db.execute('''SELECT revision,status,count(*) AS recipients FROM sandbox_campaign_queue
                WHERE campaign=? GROUP BY revision,status ORDER BY revision,status''', (campaign,))]
