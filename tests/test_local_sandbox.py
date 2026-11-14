"""Local-only prototypes: no bot import, no Render, no network, no real tariff."""
import hashlib
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sandbox_campaigns import FakeTransport, SandboxCampaigns, moscow_instant
from sandbox_payments import FakePaymentProvider, SandboxPayments
from sandbox_store import SandboxStore


class LocalSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'owned-sandbox.sqlite'
        self.store = SandboxStore.create(self.path)
        self.pay = SandboxPayments(self.store)
        self.provider = FakePaymentProvider()
        self.campaigns = SandboxCampaigns(self.store, owner=1, marketers=(2,))
        blocker = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        blocker.start()
        self.addCleanup(blocker.stop)

    def scalar(self, sql):
        with self.store.connection() as db:
            return db.execute(sql).fetchone()[0]

    def order(self, key='request', units=2, expires_at=None):
        return self.pay.create_order(20, key, 'sample_feature', 100, units, expires_at)

    def recipient(self, recipient=20, role='provider', zones=('Москва',), categories=('каток',), consent=True, blocked=False):
        self.campaigns.recipient(recipient, role, zones, categories, blocked)
        if consent:
            self.campaigns.consent(recipient, True, 100)

    def campaign(self, due=1000, **kwargs):
        campaign = self.campaigns.draft(2, 'Учебное сообщение', ('provider',), ('Москва',), **kwargs)
        preview = self.campaigns.preview(2, campaign, due)
        self.campaigns.approve(1, campaign, 1, preview['approval_token'], due)
        return campaign

    def test_owned_database_guard_and_render_rejection_never_modify_an_existing_database(self):
        other = Path(self.temp.name) / 'ordinary.sqlite'
        with sqlite3.connect(other) as db:
            db.execute('CREATE TABLE leads(id INTEGER)')
            db.execute('INSERT INTO leads VALUES (11)')
        before = hashlib.sha256(other.read_bytes()).hexdigest()
        with self.assertRaises(FileExistsError):
            SandboxStore.create(other)
        with self.assertRaises(sqlite3.OperationalError):
            SandboxStore(other)
        self.assertEqual(before, hashlib.sha256(other.read_bytes()).hexdigest())
        with patch.dict(os.environ, RENDER_SERVICE_ID='srv-test'):
            with self.assertRaises(RuntimeError):
                SandboxStore(self.path)
            with self.assertRaises(RuntimeError):
                self.order()
        with self.assertRaises(RuntimeError):
            SandboxStore.create('/var/data/forbidden-sandbox.sqlite')

    def test_order_and_confirmation_idempotency_never_double_grant_or_select_a_real_tariff(self):
        order = self.order()
        self.assertEqual(self.order(), order)
        with self.assertRaises(ValueError):
            self.pay.create_order(20, 'request', 'sample_feature', 101)
        with self.assertRaises(ValueError):
            self.pay.create_order(20, 'real', 'sample_feature', 100, currency='RUB')
        event = self.provider.paid_event(order, 'event')
        self.assertTrue(self.pay.confirm(self.provider, event))
        self.assertFalse(self.pay.confirm(self.provider, event))
        self.assertFalse(self.pay.confirm(self.provider, self.provider.paid_event(order, 'event-second')))
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_rights'), 1)
        self.assertEqual(self.scalar('SELECT remaining FROM sandbox_rights'), 2)

    def test_bad_confirmation_amount_identity_or_signature_cannot_grant_access(self):
        order = self.order()
        event = self.provider.paid_event(order)
        with self.assertRaises(ValueError):
            self.pay.confirm(self.provider, {**event, 'amount': 101})
        for key, value in (('amount', 101), ('payer', 21), ('provider_ref', 'fake_wrong')):
            with self.assertRaises(ValueError):
                self.pay.confirm(self.provider, self.provider.paid_event({**order, key: value}))
        with self.assertRaises(TypeError):
            self.pay.confirm(object(), event)
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_rights'), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_payment_events'), 0)

    def test_confirmation_is_atomic_when_grant_storage_fails(self):
        order = self.order()
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER simulate_failure BEFORE INSERT ON sandbox_rights BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pay.confirm(self.provider, self.provider.paid_event(order))
        self.assertEqual(self.scalar('SELECT status FROM sandbox_orders'), 'pending')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_payment_events'), 0)
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_rights'), 0)

    def test_concurrent_confirmation_and_consumption_preserve_one_grant_and_quota(self):
        event = self.provider.paid_event(self.order(units=1))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.pay.confirm(self.provider, event), range(2)))
        self.assertEqual(sorted(results), [False, True])
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: self.pay.consume(20, 'sample_feature', 'same-operation', 1000), range(2)))
        self.assertEqual(sum(receipt[1] for receipt in receipts), 1)
        self.assertEqual(self.scalar('SELECT remaining FROM sandbox_rights'), 0)
        self.assertIsNone(self.pay.consume(20, 'sample_feature', 'different-operation', 1000))
        reopened = SandboxPayments(SandboxStore(self.path))
        self.assertFalse(reopened.consume(20, 'sample_feature', 'same-operation', 5000)[1])

    def test_expired_or_foreign_grants_are_not_consumed(self):
        order = self.order(expires_at=1100)
        self.pay.confirm(self.provider, self.provider.paid_event(order))
        self.assertIsNone(self.pay.consume(21, 'sample_feature', 'foreign', 1000))
        self.assertIsNone(self.pay.consume(20, 'other_feature', 'wrong-feature', 1000))
        self.assertIsNone(self.pay.consume(20, 'sample_feature', 'late', 1100))
        self.assertEqual(self.scalar('SELECT remaining FROM sandbox_rights'), 2)
        with self.assertRaises(ValueError):
            self.pay.consume(20, 'sample_feature', 'invalid-clock', float('nan'))

    def test_segments_consent_and_preview_require_explicit_marketing_opt_in(self):
        self.recipient(20)
        self.recipient(21, consent=False)
        self.recipient(22, role='customer')
        self.recipient(23, zones=('Московская область',))
        self.recipient(24, blocked=True)
        self.recipient(25, categories=('манипулятор',))
        campaign = self.campaigns.draft(2, '', ('provider',), ('Москва',), categories=('каток',),
                                        attachments=[{'kind': 'photo', 'token': 'fake-photo'}])
        preview = self.campaigns.preview(2, campaign, 1000)
        self.assertEqual(preview['recipients'], [20])
        self.assertEqual(preview['payload']['text'], '')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_queue'), 0)
        with self.assertRaises(PermissionError):
            self.campaigns.approve(2, campaign, 1, preview['approval_token'], 1000)
        self.campaigns.consent(21, True, 200)
        with self.assertRaises(ValueError):
            self.campaigns.approve(1, campaign, 1, preview['approval_token'], 1000)
        fresh = self.campaigns.preview(1, campaign, 1000)
        self.assertTrue(self.campaigns.approve(1, campaign, 1, fresh['approval_token'], 1000.0))
        self.assertFalse(self.campaigns.approve(1, campaign, 1, fresh['approval_token'], 1000))
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_queue'), 2)
        self.assertEqual(self.scalar('SELECT actor FROM sandbox_campaign_approvals'), 1)

    def test_edit_invalidates_approval_keeps_revision_history_and_prevents_old_queue_send(self):
        self.recipient()
        campaign = self.campaign()
        revision = self.campaigns.edit(2, campaign, 1, 'Исправленный учебный текст')
        self.assertEqual(revision, 2)
        transport = FakeTransport()
        self.assertIsNone(self.campaigns.deliver_next(transport, 1000))
        self.assertEqual(self.scalar('SELECT status FROM sandbox_campaign_queue'), 'superseded')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_revisions'), 2)
        fresh = self.campaigns.preview(2, campaign, 1000)
        with self.assertRaises(ValueError):
            self.campaigns.approve(1, campaign, 1, fresh['approval_token'], 1000)
        self.campaigns.approve(1, campaign, 2, fresh['approval_token'], 1000)
        self.assertEqual(self.campaigns.deliver_next(transport, 1000), 'sent')
        self.assertEqual(transport.calls[0][1]['text'], 'Исправленный учебный текст')
        with self.assertRaises(ValueError):
            self.campaigns.edit(2, campaign, 2, 'Позднее изменение')

    def test_due_time_pacing_and_last_moment_opt_out_do_not_send_to_excluded_recipients(self):
        self.recipient(20); self.recipient(21); self.recipient(22)
        campaign = self.campaign()
        transport = FakeTransport()
        self.assertIsNone(self.campaigns.deliver_next(transport, 999))
        self.campaigns.consent(21, False, 999)
        self.campaigns.recipient(22, 'provider', ('Московская область',), ('каток',))
        self.assertEqual(self.campaigns.deliver_next(transport, 1000), 'sent')
        self.assertIsNone(self.campaigns.deliver_next(transport, 1000))
        self.assertEqual(self.campaigns.deliver_next(transport, 1002), 'excluded')
        self.assertEqual(self.campaigns.deliver_next(transport, 1002), 'excluded')
        self.assertEqual([recipient for recipient, _ in transport.calls], [20])
        self.assertEqual({r['status']: r['recipients'] for r in self.campaigns.report(1, campaign)}, {'sent': 1, 'excluded': 2})
        self.assertEqual(self.scalar('SELECT state FROM sandbox_campaigns'), 'completed')
        self.assertEqual(moscow_instant('2026-11-14T15:00:00'), datetime(2026, 11, 14, 12, tzinfo=timezone.utc).timestamp())
        with self.assertRaises(ValueError):
            self.campaigns.deliver_next(transport, float('nan'))

    def test_uncertain_and_crashed_reservations_are_not_repeated_after_reopen(self):
        self.recipient()
        campaign = self.campaign()
        transport = FakeTransport({20: ['crash']})
        with self.assertRaises(RuntimeError):
            self.campaigns.deliver_next(transport, 1000)
        self.assertEqual(self.scalar('SELECT status FROM sandbox_campaign_queue'), 'reserved')
        reopened = SandboxCampaigns(SandboxStore(self.path), owner=1)
        self.assertEqual(reopened.recover_reserved(1002), 1)
        self.assertIsNone(reopened.deliver_next(transport, 1003))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(reopened.report(1, campaign)[0]['status'], 'uncertain')

    def test_only_explicit_rejection_can_retry_once_and_unknown_result_stays_uncertain(self):
        self.recipient(20); self.recipient(21)
        campaign = self.campaign()
        transport = FakeTransport({20: ['rejected_429', 'sent'], 21: ['uncertain']})
        self.assertEqual(self.campaigns.deliver_next(transport, 1000), 'pending')
        self.assertIsNone(self.campaigns.deliver_next(transport, 1004))
        self.assertEqual(self.campaigns.deliver_next(transport, 1005), 'sent')
        self.assertEqual(self.campaigns.deliver_next(transport, 1007), 'uncertain')
        self.assertIsNone(self.campaigns.deliver_next(transport, 1010))
        self.assertEqual([recipient for recipient, _ in transport.calls], [20, 20, 21])
        self.assertEqual({r['status']: r['recipients'] for r in self.campaigns.report(1, campaign)}, {'sent': 1, 'uncertain': 1})
        with self.assertRaises(TypeError):
            self.campaigns.deliver_next(object(), 1011)

    def test_concurrent_claimers_do_not_duplicate_fake_delivery(self):
        self.recipient()
        self.campaign()
        transport = FakeTransport()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.campaigns.deliver_next(transport, 1000), range(2)))
        self.assertEqual(results.count('sent'), 1)
        self.assertEqual(len(transport.calls), 1)
        with self.store.connection() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_verified_failure_retry_cancel_and_late_paid_never_grant_cancelled_orders(self):
        order = self.order()
        failed = self.provider.event(order, 'failed', 'failed-attempt')
        self.assertTrue(self.pay.apply_event(self.provider, failed))
        self.assertFalse(self.pay.apply_event(self.provider, failed))
        self.assertEqual(self.pay.get_order(20, order['id'])['status'], 'failed')
        self.assertEqual(self.order()['status'], 'failed')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_rights'), 0)
        self.assertTrue(self.pay.confirm(self.provider, self.provider.paid_event(order)))
        self.assertEqual(self.pay.get_order(20, order['id'])['status'], 'paid')
        with self.assertRaises(ValueError):
            self.pay.cancel(20, order['id'], 'cancel-paid')
        second = self.order('second')
        with self.assertRaises(ValueError):
            self.pay.cancel(21, second['id'], 'foreign')
        self.assertTrue(self.pay.cancel(20, second['id'], 'cancel-second'))
        self.assertFalse(self.pay.cancel(20, second['id'], 'cancel-second'))
        with self.assertRaises(ValueError):
            self.pay.cancel(20, order['id'], 'cancel-second')
        with self.assertRaises(ValueError):
            self.pay.confirm(self.provider, self.provider.paid_event(second))
        self.assertEqual(self.pay.get_order(20, second['id'])['status'], 'cancelled')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_rights'), 1)

    def test_resource_reopen_rechecks_authorization_and_never_debits_twice_after_restart(self):
        order = self.order(units=2, expires_at=1100)
        self.pay.confirm(self.provider, self.provider.paid_event(order))
        with self.assertRaises(PermissionError):
            self.pay.access_resource(20, 'sample_feature', 'fixture-11', 1000, authorized=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda _: self.pay.access_resource(
                20, 'sample_feature', 'fixture-11', 1000, authorized=True), range(2)))
        self.assertEqual(sum(r[1] for r in receipts), 1)
        reopened = SandboxPayments(SandboxStore(self.path))
        self.assertEqual(reopened.access_resource(20, 'sample_feature', 'fixture-11', 1200, authorized=True),
                         (order['id'], False))
        with self.assertRaises(PermissionError):
            reopened.access_resource(20, 'sample_feature', 'fixture-11', 1200, authorized=False)
        self.assertIsNone(reopened.access_resource(21, 'sample_feature', 'fixture-11', 1000, authorized=True))
        self.assertIsNone(reopened.access_resource(20, 'sample_feature', 'fixture-12', 1200, authorized=True))
        self.assertEqual(self.scalar('SELECT remaining FROM sandbox_rights'), 1)

    def test_full_fake_refund_is_atomic_idempotent_and_revokes_new_and_old_resource_receipts(self):
        order = self.order()
        self.pay.confirm(self.provider, self.provider.paid_event(order, 'paid'))
        self.pay.access_resource(20, 'sample_feature', 'fixture-11', 1000, authorized=True)
        refund = self.provider.event(order, 'refunded', 'refund')
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_refund BEFORE UPDATE OF remaining ON sandbox_rights "
                       "BEGIN SELECT RAISE(ABORT,'fixture refund failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.pay.apply_event(self.provider, refund)
        self.assertEqual(self.pay.get_order(20, order['id'])['status'], 'paid')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_payment_refunds'), 0)
        # Preserve this fault-injection fixture; use a separate order database
        # for the successful path rather than removing the trigger or records.
        fresh = SandboxPayments(SandboxStore.create(Path(self.temp.name)/'refund-success.sqlite'))
        order = fresh.create_order(20, 'refund-order', 'sample_feature', 100, 2)
        paid = self.provider.paid_event(order, 'paid-success')
        fresh.confirm(self.provider, paid)
        fresh.access_resource(20, 'sample_feature', 'fixture-11', 1000, authorized=True)
        event = self.provider.event(order, 'refunded', 'refund-success')
        self.assertTrue(fresh.apply_event(self.provider, event))
        self.assertFalse(fresh.apply_event(self.provider, event))
        self.assertFalse(fresh.apply_event(self.provider, self.provider.event(order, 'refunded', 'same-refund-new-event')))
        self.assertFalse(fresh.confirm(self.provider, paid))
        self.assertEqual(fresh.get_order(20, order['id'])['status'], 'refunded')
        self.assertEqual(fresh.create_order(20, 'refund-order', 'sample_feature', 100, 2)['status'], 'refunded')
        self.assertIsNone(fresh.access_resource(20, 'sample_feature', 'fixture-11', 1001, authorized=True))
        self.assertIsNone(fresh.access_resource(20, 'sample_feature', 'fixture-12', 1001, authorized=True))
        with self.assertRaises(ValueError):
            fresh.confirm(self.provider, self.provider.paid_event(order, 'late-new-paid'))

    def test_refund_and_cancel_event_identity_signature_and_order_scope_are_verified(self):
        order = self.order()
        with self.assertRaises(ValueError):
            self.pay.apply_event(self.provider, self.provider.event(order, 'refunded'))
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_payment_events'), 0)
        event = self.provider.event(order, 'cancelled', 'cancel-event')
        with self.assertRaises(ValueError):
            self.pay.apply_event(self.provider, {**event, 'status': 'refunded'})
        with self.assertRaises(ValueError):
            self.pay.confirm(self.provider, event)
        self.assertTrue(self.pay.apply_event(self.provider, event))
        self.assertFalse(self.pay.apply_event(self.provider, event))
        self.assertEqual(self.pay.get_order(20, order['id'])['status'], 'cancelled')
        with self.assertRaises(ValueError):
            self.pay.get_order(21, order['id'])
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_rights'), 0)

    def test_pause_resume_keep_approval_and_queue_and_recheck_consent_before_next_claim(self):
        self.recipient(20); self.recipient(21)
        campaign = self.campaign()
        transport = FakeTransport()
        with self.assertRaises(PermissionError):
            self.campaigns.pause(2, campaign, 1, 1000)
        self.assertTrue(self.campaigns.pause(1, campaign, 1, 1000))
        self.assertFalse(self.campaigns.pause(1, campaign, 1, 1000))
        reopened = SandboxCampaigns(SandboxStore(self.path), owner=1)
        self.assertIsNone(reopened.deliver_next(transport, 1000))
        with self.assertRaises(ValueError):
            reopened.resume(1, campaign, 2, 1000)
        self.campaigns.consent(20, False, 1000)
        self.assertTrue(reopened.resume(1, campaign, 1, 1001))
        self.assertFalse(reopened.resume(1, campaign, 1, 1001))
        self.assertEqual(reopened.deliver_next(transport, 1001), 'excluded')
        self.assertEqual(reopened.deliver_next(transport, 1001), 'sent')
        self.assertEqual([r for r, _ in transport.calls], [21])
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_approvals'), 1)
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_controls'), 2)

    def test_explicit_retry_after_survives_pause_restart_and_second_rejection_is_terminal(self):
        self.recipient()
        campaign = self.campaign()
        transport = FakeTransport({20: [{'status': 'rejected_429', 'retry_after': 17},
                                        {'status': 'rejected_429', 'retry_after': 30}]})
        self.assertEqual(self.campaigns.deliver_next(transport, 1000), 'pending')
        self.campaigns.pause(1, campaign, 1, 1001)
        reopened = SandboxCampaigns(SandboxStore(self.path), owner=1)
        reopened.resume(1, campaign, 1, 1002)
        self.assertIsNone(reopened.deliver_next(transport, 1016))
        self.assertEqual(reopened.deliver_next(transport, 1017), 'failed')
        self.assertIsNone(reopened.deliver_next(transport, 1100))
        self.assertEqual(len(transport.calls), 2)

    def test_late_return_after_explicit_uncertain_recovery_cannot_requeue_or_resend(self):
        self.recipient()
        campaign = self.campaign()
        transport = FakeTransport({20: ['rejected_429', 'sent']})
        original = transport.send
        def recover_during_fake_io(recipient, payload):
            result = original(recipient, payload)
            self.assertEqual(self.campaigns.recover_reserved(1001), 1)
            return result
        transport.send = recover_during_fake_io
        self.assertEqual(self.campaigns.deliver_next(transport, 1000), 'uncertain')
        self.assertIsNone(self.campaigns.deliver_next(transport, 1100))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.campaigns.report(1, campaign)[0]['status'], 'uncertain')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_events'), 2)
