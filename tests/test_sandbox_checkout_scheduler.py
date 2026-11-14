"""Checkout/domain and scheduler recovery on restored synthetic business data."""
import asyncio
import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('BOT_TOKEN', '99:TEST')
os.environ.setdefault('ADMIN_IDS', '1,2')
os.environ.setdefault('LEADS_CHANNEL_ID', '-100123')

import admin_access
import bot
import customer_contacts
import provider_registry
import storage
from aiogram.types import Update
from sandbox_admin_ui import LocalAdmin
from sandbox_campaigns import FakeTransport, SandboxCampaigns
from sandbox_checkout import BusinessCopy, SandboxCheckout, TestOffer, TestResource
from sandbox_local_telegram import local_bot, local_dispatcher
from sandbox_payments import FakePaymentProvider
from sandbox_scheduler import SandboxScheduler
from sandbox_store import SandboxBusy, SandboxStore


def crash_sender(path, trace):
    store = SandboxStore(path)
    campaigns = SandboxCampaigns(store, 1)
    transport = FakeTransport()
    def send(_recipient, _payload):
        with open(trace, 'a') as f:
            f.write('one possible fake delivery\n'); f.flush(); os.fsync(f.fileno())
        os._exit(17)
    transport.send = send
    SandboxScheduler(campaigns, transport).tick(2000)


class CheckoutSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, snapshot = root/'business.sqlite', root/'snapshot.sqlite'
        with patch.object(storage, 'DB_PATH', str(self.source)):
            await bot.init_db()
            await bot.SQLiteDraftStorage(str(self.source)).init_schema()
            await admin_access.assign_role(1, 2, 'marketer', 'fixture-role', owner_id=1)
        await provider_registry.register_provider(21, 'Учебный', '+79990000000', True, str(self.source))
        await provider_registry.add_equipment(21, 'каток', 'Учебная машина', 'Москва', 'весь регион', str(self.source))
        with sqlite3.connect(self.source) as db:
            for lead, state in ((11, 'closed'), (12, 'published')):
                db.execute('INSERT INTO leads(id,customer_id,customer_name,equipment,place,work_date,details,phone,status,created_at) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (lead, 20, 'Учебный', 'каток', 'Москва', 'fixture', '', '+79990000000', state, 'fixture'))
            db.execute('INSERT INTO customer_contact_access VALUES (21,11,\'+79990000000\')')
            db.execute('INSERT OR IGNORE INTO bot_visitors VALUES (21)')
            db.execute('INSERT INTO form_drafts VALUES (\'fixture-provider\',\'Register:name\',\'{"keep":true}\')')
            db.execute('INSERT INTO form_drafts VALUES (\'fixture-customer\',\'LeadForm:region\',\'{"saved":"field"}\')')
            db.execute("INSERT INTO offers(lead_id,provider_id,provider_name,username,price,comment,phone,created_at,delivery_status) "
                       "VALUES (12,21,'Учебный','','100','','+79990000000','fixture','unconfirmed')")
            db.execute("INSERT INTO provider_notification_reservations VALUES (12,21,'uncertain')")
        with sqlite3.connect(self.source) as src, sqlite3.connect(snapshot) as dst:
            src.backup(dst)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.business = BusinessCopy.restore(snapshot, root/'restored-business.sqlite', owner=1)
        self.store = SandboxStore.create(root/'sidecar.sqlite')
        self.offers = (TestOffer('unit', 'Учебная функция', 'sample_feature', 'function', 100),
                       TestOffer('pack', 'Учебный пакет', 'sample_feature', 'package', 100, 2),
                       TestOffer('period', 'Учебный срок', 'sample_period', 'period', 100, 1, 1000))
        self.resources = (TestResource('one', 'sample_feature'), TestResource('two', 'sample_feature'),
                          TestResource('a', 'sample_period'), TestResource('b', 'sample_period'), TestResource('c', 'sample_period'))
        self.checkout = SandboxCheckout(self.store, self.business, self.offers, self.resources, enforce_paid=True)
        self.transport = FakeTransport()
        self.campaigns = SandboxCampaigns(self.store, 1, (2,))
        self.scheduler = SandboxScheduler(self.campaigns, self.transport)
        self.network = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)

    def scalar(self, sql):
        with self.store.connection() as db:
            return db.execute(sql).fetchone()[0]

    async def checked_order(self, offer='pack', key='fixture'):
        review = await self.checkout.begin(21, offer, key)
        order = await self.checkout.submit(21, review['id'], review['token'])
        return review, order

    def campaign(self, recipients=(21,), due=2000):
        for uid in recipients:
            self.campaigns.recipient(uid, 'provider', ('Москва',), ('каток',))
            self.campaigns.consent(uid, True, 1000)
        cid = self.campaigns.draft(1, 'Учебная кампания', ('provider',), ('Москва',), ('каток',))
        p = self.campaigns.preview(1, cid, due)
        self.campaigns.approve(1, cid, 1, p['approval_token'], due)
        return cid

    async def test_checkout_atomicity_forgery_quota_reopen_and_business_rows_preserved(self):
        before = hashlib.sha256(self.business.path.read_bytes()).hexdigest()
        review = await self.checkout.begin(21, 'pack', 'same')
        with self.assertRaises(ValueError):
            await self.checkout.submit(21, review['id'], 'forged')
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_orders'), 0)
        orders = await asyncio.gather(*(self.checkout.submit(21, review['id'], review['token']) for _ in range(2)))
        self.assertEqual(orders[0]['id'], orders[1]['id'])
        self.assertIsNone(await self.checkout.open_resource(21, 'one', 2000))
        provider = FakePaymentProvider()
        with self.assertRaises(ValueError):
            self.checkout.payments.confirm(provider, {**provider.paid_event(orders[0]), 'amount': 999})
        self.assertTrue(await self.checkout.simulate(21, review['id'], 'paid', 2000))
        self.assertFalse(await self.checkout.simulate(21, review['id'], 'paid', 2500))
        first = await self.checkout.open_resource(21, 'one', 2000)
        self.assertTrue(first[1])
        reopened = SandboxCheckout(SandboxStore(self.store.path), self.business, self.offers, self.resources, enforce_paid=True)
        self.assertEqual(await reopened.open_resource(21, 'one', 9999), (orders[0]['id'], False))
        self.assertTrue((await reopened.open_resource(21, 'two', 2001))[1])
        self.assertEqual(self.scalar('SELECT remaining FROM sandbox_rights'), 0)
        self.assertEqual(before, hashlib.sha256(self.business.path.read_bytes()).hexdigest())
        self.assertEqual(self.source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())

    async def test_atomic_submit_rolls_back_order_when_checkout_state_cannot_commit(self):
        review = await self.checkout.begin(21, 'unit', 'atomic')
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER fail_checkout BEFORE UPDATE ON sandbox_checkouts BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.checkout.submit(21, review['id'], review['token'])
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_orders'), 0)
        self.assertEqual(self.scalar('SELECT state FROM sandbox_checkouts'), 'review')

    async def test_period_starts_after_confirmation_never_resets_and_refund_revokes(self):
        review, order = await self.checked_order('period')
        await self.checkout.simulate(21, review['id'], 'paid', 2000)
        await self.checkout.simulate(21, review['id'], 'paid', 2500)
        self.assertEqual(self.scalar('SELECT expires_at FROM sandbox_rights'), 3000)
        self.assertTrue((await self.checkout.open_resource(21, 'a', 2999))[1])
        self.assertTrue((await self.checkout.open_resource(21, 'b', 2999))[1])
        self.assertEqual(self.scalar('SELECT remaining FROM sandbox_rights'), 1)
        self.assertIsNone(await self.checkout.open_resource(21, 'c', 3000))
        self.assertEqual(await self.checkout.open_resource(21, 'a', 4000), (order['id'], False))
        await self.checkout.simulate(21, review['id'], 'refunded', 4001)
        self.assertIsNone(await self.checkout.open_resource(21, 'a', 4002))

    async def test_roles_restrictions_terms_cancel_and_free_contacts_are_independent(self):
        with self.assertRaises(PermissionError):
            await self.checkout.begin(20, 'unit', 'not-provider')
        review, order = await self.checked_order('unit')
        await self.checkout.cancel(21, review['id'])
        with self.assertRaises(ValueError):
            await self.checkout.simulate(21, review['id'], 'paid', 2000)
        changed = SandboxCheckout(self.store, self.business,
            (TestOffer('pack', 'Другие условия', 'sample_feature', 'package', 200, 2),), self.resources)
        pending = await self.checkout.begin(21, 'pack', 'price-change')
        with self.assertRaises(ValueError):
            await changed.submit(21, pending['id'], pending['token'])
        free = SandboxCheckout(self.store, self.business, self.offers, self.resources)
        self.assertEqual(await free.open_resource(21, 'one', 2000), ('free', False))
        # The actual existing function stays free, including historical closed
        # lead access; no paid gate or checkout import is added to that module.
        self.assertEqual(await customer_contacts.open_contact(str(self.business.path), 21, 11), '+79990000000')
        self.assertEqual(await customer_contacts.open_contact(str(self.business.path), 21, 12), '+79990000000')
        with sqlite3.connect(self.business.path) as db:
            db.execute("INSERT INTO user_access_events(user_id,scopes_json,reason,actor_id,request_id,created_at) "
                       "VALUES (21,'[\"offers\"]','fixture',1,'fixture-block','fixture')")
        with self.assertRaises(PermissionError):
            await free.open_resource(21, 'one', 2001)
        with self.assertRaises(PermissionError):
            await customer_contacts.open_contact(str(self.business.path), 21, 11)
        self.assertEqual(self.source_hash, hashlib.sha256(self.source.read_bytes()).hexdigest())

    async def test_scheduler_is_due_confirmed_scoped_paced_and_rechecks_segment_opt_out(self):
        cid = self.campaign((21, 22, 23))
        self.assertIsNone(self.scheduler.tick(1999)['outcome'])
        self.campaigns.consent(22, False, 1999)
        self.campaigns.recipient(23, 'provider', ('Московская область',), ('каток',))
        self.transport.outcomes[21] = [{'status': 'rejected_429', 'retry_after': 9}, 'sent']
        self.assertEqual(self.scheduler.tick(2000)['outcome'], 'pending')
        reopened = SandboxScheduler(SandboxCampaigns(SandboxStore(self.store.path), 1), self.transport)
        self.assertIsNone(reopened.tick(2008)['outcome'])
        self.assertEqual(reopened.tick(2009)['outcome'], 'sent')
        self.assertEqual(reopened.tick(2011)['outcome'], 'excluded')
        self.assertEqual(reopened.tick(2011)['outcome'], 'excluded')
        self.assertEqual([uid for uid, _ in self.transport.calls], [21, 21])
        unapproved = self.campaigns.draft(1, 'Неподтверждённая', ('provider',), ('Москва',))
        self.assertIsNone(reopened.tick(9999, campaign=unapproved)['outcome'])

    async def test_coordinator_denies_live_recovery_and_pause_stops_new_claims(self):
        cid = self.campaign((21, 22))
        entered, release = threading.Event(), threading.Event()
        original = self.transport.send
        def blocked_send(uid, payload):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('fixture coordination timeout')
            return original(uid, payload)
        self.transport.send = blocked_send
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(self.scheduler.tick, 2000)
            self.assertTrue(entered.wait(2))
            other = SandboxCampaigns(SandboxStore(self.store.path), 1)
            try:
                with self.assertRaises(SandboxBusy):
                    other.recover_reserved(2001)
                self.assertTrue(SandboxScheduler(other, FakeTransport()).tick(2001)['busy'])
                other.pause(1, cid, 1, 2001)
            finally:
                release.set()
            self.assertEqual(running.result(timeout=3)['outcome'], 'sent')
        self.assertIsNone(self.scheduler.tick(2002)['outcome'])
        self.assertEqual(len(self.transport.calls), 1)
        other.resume(1, cid, 1, 2003)
        self.assertEqual(self.scheduler.tick(2003)['outcome'], 'sent')

    async def test_real_process_crash_releases_lock_and_quarantines_without_duplicate(self):
        cid = self.campaign()
        trace = Path(self.temp.name)/'fake-trace.txt'
        child = multiprocessing.get_context('fork').Process(target=crash_sender, args=(str(self.store.path), str(trace)))
        child.start(); child.join(5)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 17)
        result = self.scheduler.tick(2002)
        self.assertEqual(result['recovered'], 1)
        self.assertIsNone(result['outcome'])
        self.assertIsNone(self.scheduler.tick(5000)['outcome'])
        self.assertEqual(len(trace.read_text().splitlines()), 1)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_quarantine'), 1)
        self.assertEqual(self.campaigns.report(1, cid)[0]['status'], 'uncertain')

    async def test_timeout_is_quarantined_and_async_local_worker_stops_cleanly(self):
        self.campaign((21, 22))
        self.transport.outcomes[21] = ['timeout']
        with self.assertRaises(TimeoutError):
            self.scheduler.tick(2000)
        self.assertEqual(self.scalar('SELECT count(*) FROM sandbox_campaign_quarantine'), 1)
        self.assertEqual(self.scheduler.tick(2002)['outcome'], 'sent')
        self.assertIsNone(self.scheduler.tick(9999)['outcome'])
        stop = asyncio.Event()
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            if calls >= 2:
                stop.set()
            return 10000+calls
        await asyncio.wait_for(self.scheduler.run(stop, clock, interval=0.01), timeout=2)
        self.assertEqual([uid for uid, _ in self.transport.calls], [21, 22])

    async def test_local_checkout_dispatcher_and_real_role_revocation(self):
        admin = LocalAdmin(self.store, 1, transport=FakeTransport())
        telegram = local_bot()
        self.addAsyncCleanup(telegram.session.close)
        dp = local_dispatcher(admin, lambda: 2000, checkout=self.checkout, business=self.business)
        sequence = 100
        async def message(uid, text):
            nonlocal sequence
            sequence += 1
            await dp.feed_update(telegram, Update.model_validate({'update_id': sequence, 'message': {
                'message_id': sequence, 'date': 1, 'chat': {'id': uid, 'type': 'private'},
                'from': {'id': uid, 'is_bot': False, 'first_name': 'Учебный'}, 'text': text}}))
        def last(uid):
            return next(m for m in reversed(telegram.session.calls) if m.__api_method__=='sendMessage' and m.chat_id==uid and m.reply_markup)
        async def click(uid, label):
            nonlocal sequence
            data = next(b.callback_data for row in last(uid).reply_markup.inline_keyboard for b in row if b.text==label)
            sequence += 1
            await dp.feed_update(telegram, Update.model_validate({'update_id': sequence, 'callback_query': {
                'id':str(sequence),'chat_instance':'local','from':{'id':uid,'is_bot':False,'first_name':'Учебный'},'data':data,
                'message':{'message_id':1,'date':1,'chat':{'id':uid,'type':'private'}}}}))
        await message(21, '/checkout')
        await click(21, 'Учебный пакет · 100 TEST')
        await click(21, 'Подтвердить оформление')
        self.assertIn('Статус: pending', last(21).text)
        await click(21, 'Подставной провайдер: оплатить')
        await click(21, 'Открыть one')
        await click(21, 'Открыть one')
        self.assertEqual(self.scalar('SELECT remaining FROM sandbox_rights'), 1)
        await message(2, '/start')
        self.assertIn(2, admin.marketers)
        with patch.object(storage, 'DB_PATH', str(self.business.path)):
            await admin_access.assign_role(1, 2, 'none', 'fixture-revoke', owner_id=1)
        await click(2, 'Кампании')
        self.assertNotIn(2, admin.marketers)

    async def test_dispatcher_lifecycle_automatically_runs_due_fake_campaign_only_once(self):
        self.campaign()
        admin = LocalAdmin(self.store, 1, transport=self.transport)
        telegram = local_bot()
        self.addAsyncCleanup(telegram.session.close)
        current = [1000]
        dp = local_dispatcher(admin, lambda: current[0], checkout=self.checkout,
                              business=self.business, scheduler_interval=0.01)
        await dp.emit_startup(bot=telegram)
        try:
            await asyncio.sleep(0.03)
            self.assertEqual(self.transport.calls, [])
            current[0] = 2000
            for _ in range(100):
                if self.transport.calls:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(self.transport.calls), 1)
            with self.assertRaises(RuntimeError):
                await dp.emit_startup(bot=telegram)
        finally:
            await dp.emit_shutdown()
        self.assertIsNone(admin.scheduler.tick(9000)['outcome'])
