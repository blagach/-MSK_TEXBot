"""Moderation races: synthetic SQLite and fake Telegram only; no service access."""
import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramRetryAfter, TelegramNetworkError
from aiogram.methods import SendMessage
from types import SimpleNamespace as R
import test_inbot_publication as fixtures
import bot
import storage
import lead_journey as journey
import notification_reservations as ledger
import lead_delivery_guard as guard
import admin_access


class ModerationRestoreDeliveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.InBotPublicationTests.asyncSetUp
    rows = fixtures.InBotPublicationTests.rows
    subscribe = fixtures.InBotPublicationTests.subscribe
    message_for = fixtures.InBotPublicationTests.message_for

    async def active(self):
        await self.subscribe()
        lid,_=await bot.save_lead_from_draft(self.customer,self.data['phone'],self.data)
        self.assertTrue(await bot.lead_publication.activate_new(lid,501))
        return lid

    async def close(self,lid,reason='TEST нарушение'):
        return await journey.change_lifecycle(self.path,lid,1,admin=True,reason=reason)

    async def restore(self,lid,revision=None):
        if revision is None:revision=(await journey.moderation_snapshot(self.path,lid))[0]
        return await journey.restore_closed(self.path,lid,1,'TEST ошибочное закрытие',revision,legacy_admins={1})

    def status(self,lid):return self.rows('SELECT status FROM leads WHERE id=?',(lid,))[0][0]

    def cancelled(self,lid):
        return [json.loads(r[0]) for r in self.rows("SELECT details FROM admin_action_log WHERE target_id=? AND action='provider_notification_cancelled'",(lid,))]

    async def test_repeated_cycles_keep_history_search_offers_and_no_restoration_sends(self):
        lid=await self.active();await self.close(lid)
        before=self.rows("SELECT * FROM lead_lifecycle_events WHERE action='closed'")
        revision,_=await journey.moderation_snapshot(self.path,lid)
        provider=self.message_for(701)
        await bot.show_matching_leads(provider,701)
        self.assertIn('пока нет',provider.answer.await_args.args[0])
        self.assertIsNone(await storage.save_offer(lid,701,'TEST','','100','','+79990000001'))
        await bot.open_offer(provider,self.state,lid,701)
        self.assertTrue(any('недоступна' in c.args[0] for c in provider.answer.await_args_list))
        self.assertTrue(await self.restore(lid,revision))
        self.assertFalse(await self.restore(lid,revision))
        await bot.show_matching_leads(provider,701)
        self.assertIn('№1',provider.answer.await_args.args[0])
        self.assertIsNotNone(await storage.save_offer(lid,701,'TEST','','100','','+79990000001'))
        self.assertTrue(await self.close(lid,'TEST второе закрытие'))
        self.assertFalse(await self.restore(lid,revision),'Old restore control cannot restore another cycle')
        self.assertTrue(await self.restore(lid))
        self.assertEqual(self.rows("SELECT * FROM lead_lifecycle_events WHERE action='closed'"),before)
        _,history=await journey.moderation_snapshot(self.path,lid)
        self.assertEqual([r['action'] for r in history],['lead_restore','lead_close','lead_restore','lead_close'])
        self.assertTrue(all(r['created_at'] and r['actor_id']==1 and json.loads(r['details'])['reason'] for r in history))
        self.telegram.send_message.assert_not_awaited()

    async def test_permissions_rechecked_at_commit_pause_and_blocked_customer(self):
        lid=await self.active();await self.close(lid);rev,_=await journey.moderation_snapshot(self.path,lid)
        with self.assertRaises(PermissionError):
            await journey.restore_closed(self.path,lid,501,'TEST',rev,legacy_admins={1})
        with self.assertRaises(ValueError):await journey.restore_closed(self.path,lid,1,' ',rev,legacy_admins={1})
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO admin_role_events VALUES (1,1,'none',9,'TEST revoke','2099')")
        with self.assertRaises(PermissionError):await self.restore(lid)
        with sqlite3.connect(self.path) as c:c.execute("INSERT INTO admin_role_events VALUES (2,1,'moderator',9,'TEST grant','2099')")
        self.assertTrue(await bot.lead_publication.set_mode(1,1,False,0))
        self.assertFalse(await self.restore(lid))
        mode=await bot.lead_publication.mode();await bot.lead_publication.set_mode(1,1,True,mode['revision'])
        with patch.object(bot.user_access,'allowed_in',AsyncMock(return_value=False)):
            self.assertFalse(await self.restore(lid))
        self.assertEqual(self.status(lid),'closed')

    async def test_close_between_reservation_and_send_cancels_durably(self):
        lid=await self.active();original=ledger.reserve
        async def reserve_then_close(*a,**kw):
            ok=await original(*a,**kw)
            if ok:self.assertTrue(await self.close(lid))
            return ok
        with patch.object(ledger,'reserve',reserve_then_close):
            await bot.notify_test_providers(self.telegram,await bot.get_lead(lid))
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(len(self.cancelled(lid)),1)
        self.assertTrue(await self.restore(lid))
        await bot.init_db()
        await bot.notify_test_providers(self.telegram,await bot.get_lead(lid))
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(len(self.cancelled(lid)),1)
        self.assertFalse(await ledger.reserve(lid,701,self.path))

    async def test_all_reserved_queue_items_cancel_and_are_not_unknown(self):
        lid=await self.active();await self.subscribe(702)
        for pid in (701,702):self.assertTrue(await ledger.reserve(lid,pid,self.path))
        self.assertTrue(await self.close(lid))
        self.assertEqual({r['provider_id'] for r in self.cancelled(lid)},{701,702})
        for pid in (701,702):
            self.assertIsNone(await bot.send_provider_notification(self.telegram,pid,'TEST',lead_id=lid,revision=0))
        self.telegram.send_message.assert_not_awaited()

    async def test_close_and_restore_during_429_delay_does_not_resume_send(self):
        lid=await self.active();self.assertTrue(await ledger.reserve(lid,701,self.path))
        self.telegram.send_message.side_effect=TelegramRetryAfter(SendMessage(chat_id=701,text='TEST'),'rejected',retry_after=1)
        async def delay(_):
            self.assertTrue(await self.close(lid))
            self.assertTrue(await self.restore(lid))
        with patch.object(bot.asyncio,'sleep',delay):
            self.assertIsNone(await bot.send_provider_notification(self.telegram,701,'TEST',lead_id=lid,revision=0))
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual(len(self.cancelled(lid)),1)

    async def test_old_batch_revision_cannot_send_unreserved_recipients_after_restore(self):
        lid=await self.active();await self.close(lid);await self.restore(lid)
        self.assertTrue(await ledger.reserve(lid,701,self.path))
        self.assertIsNone(await bot.send_provider_notification(self.telegram,701,'TEST',lead_id=lid,revision=0))
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(len(self.cancelled(lid)),1)

    async def test_inflight_send_completes_before_closure_commits(self):
        lid=await self.active();await ledger.reserve(lid,701,self.path)
        entered=asyncio.Event();finish=asyncio.Event()
        async def accept(*a,**kw):
            self.assertEqual(self.status(lid),'published');entered.set();await finish.wait()
            return R(message_id=901)
        self.telegram.send_message.side_effect=accept
        send=asyncio.create_task(bot.send_provider_notification(self.telegram,701,'TEST',lead_id=lid,revision=0))
        await asyncio.wait_for(entered.wait(),2)
        closing=asyncio.create_task(self.close(lid))
        # Yield to the closer without a timed sleep or real network.
        await asyncio.wait_for(asyncio.shield(asyncio.create_task(self._yield_once())),2)
        self.assertFalse(closing.done());self.assertEqual(self.status(lid),'published')
        finish.set();self.assertEqual(await asyncio.gather(send,closing),[True,True])
        self.assertEqual(self.rows('SELECT status FROM provider_notification_reservations'),[('confirmed',)])
        self.assertEqual(self.cancelled(lid),[])
        self.assertEqual(self.status(lid),'closed')

    async def _yield_once(self):await asyncio.sleep(0)

    async def test_closure_wins_guard_before_send(self):
        lid=await self.active();await ledger.reserve(lid,701,self.path)
        async with guard.for_lead(self.path,lid):
            closing=asyncio.create_task(self.close(lid));await self._yield_once()
            sending=asyncio.create_task(bot.send_provider_notification(self.telegram,701,'TEST',lead_id=lid,revision=0))
            await self._yield_once()
        self.assertEqual(await asyncio.gather(closing,sending),[True,None])
        self.telegram.send_message.assert_not_awaited()

    async def test_unknown_delivery_stays_unknown_across_close_restore_and_restart(self):
        lid=await self.active();await ledger.reserve(lid,701,self.path)
        self.telegram.send_message.side_effect=TelegramNetworkError(SendMessage(chat_id=701,text='TEST'),'unknown')
        self.assertFalse(await bot.send_provider_notification(self.telegram,701,'TEST',lead_id=lid,revision=0))
        await self.close(lid);await self.restore(lid);await bot.init_db()
        await bot.notify_test_providers(self.telegram,await bot.get_lead(lid))
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual(self.rows('SELECT status FROM provider_notification_reservations'),[('uncertain',)])
        self.assertEqual(self.cancelled(lid),[])

    async def test_legacy_reserved_and_interrupted_attempt_are_never_relabelled_or_retried(self):
        lid=await self.active();await self.subscribe(702)
        with sqlite3.connect(self.path) as c:
            c.execute("INSERT INTO provider_notification_reservations VALUES (?,702,'reserved')",(lid,))
        await ledger.reserve(lid,701,self.path)
        self.assertTrue(await ledger.may_send(lid,701,self.path,0))  # Process death after durable attempt, before outcome.
        await self.close(lid);await self.restore(lid);await bot.init_db()
        self.assertEqual(self.cancelled(lid),[])
        await bot.notify_test_providers(self.telegram,await bot.get_lead(lid))
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(self.rows('SELECT status FROM provider_notification_reservations'),[('reserved',),('reserved',)])

    async def test_old_contact_buttons_deny_closed_card_without_erasing_saved_contacts(self):
        lid=await self.active()
        phone=await bot.customer_contacts.open_contact(self.path,701,lid)
        await self.close(lid)
        m=self.message_for(701)
        cb=R(data=f'customer_contact:{lid}',from_user=R(id=701),message=m,answer=AsyncMock())
        await bot.reveal_customer_contact(cb)
        self.assertIn('недоступна',cb.answer.await_args.args[0])
        await bot.start_offer(m,self.state,R(args=f'contact_{lid}'))
        self.assertIn('недоступна',m.answer.await_args.args[0])
        self.assertEqual(await bot.customer_contacts.open_contact(self.path,701,lid,saved_only=True),phone)

    async def test_audit_failure_rolls_back_close_restore_and_cancellations(self):
        lid=await self.active();await ledger.reserve(lid,701,self.path)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TRIGGER fail_close BEFORE INSERT ON admin_action_log WHEN NEW.action='lead_close' BEGIN SELECT RAISE(ABORT,'TEST'); END")
        with self.assertRaises(sqlite3.IntegrityError):await self.close(lid)
        self.assertEqual(self.status(lid),'published');self.assertEqual(self.cancelled(lid),[])
        with sqlite3.connect(self.path) as c:c.execute('DROP TRIGGER fail_close')
        await self.close(lid)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TRIGGER fail_restore BEFORE INSERT ON admin_action_log WHEN NEW.action='lead_restore' BEGIN SELECT RAISE(ABORT,'TEST'); END")
        with self.assertRaises(sqlite3.IntegrityError):await self.restore(lid)
        self.assertEqual(self.status(lid),'closed')

    async def test_admin_ui_restore_confirmation_reason_and_revoked_rights(self):
        lid=await self.active();await self.close(lid)
        m=self.message_for(1);lead=await bot.get_lead(lid)
        await bot.show_customer_lead(m,lead,False,True)
        buttons=[b for row in m.answer.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        button=next(b for b in buttons if b.text=='Восстановить заявку')
        cb=R(data=button.callback_data,from_user=R(id=1),message=m,answer=AsyncMock())
        await bot.lead_card_action(cb,self.state,self.telegram)
        self.assertEqual((await self.state.get_data())['closing_action'],'restore')
        self.assertEqual(await self.state.get_state(),bot.CloseLead.reason.state)
        m.text='TEST восстановление';await bot.close_lead_reason(m,self.state)
        data=await self.state.get_data();m.message_id=data['form_keyboard_id']
        cb.data='lead_close:send'
        with sqlite3.connect(self.path) as c:c.execute("INSERT INTO admin_role_events VALUES (1,1,'none',9,'TEST revoke','2099')")
        await bot.close_lead_confirmation(cb,self.state,self.telegram)
        self.assertEqual(self.status(lid),'closed')
        with sqlite3.connect(self.path) as c:c.execute("INSERT INTO admin_role_events VALUES (2,1,'moderator',9,'TEST grant','2099')")
        await bot.show_closing_step(m,self.state)
        m.message_id=(await self.state.get_data())['form_keyboard_id']
        await bot.close_lead_confirmation(cb,self.state,self.telegram)
        self.assertEqual(self.status(lid),'published')
        self.telegram.send_message.assert_not_awaited()
