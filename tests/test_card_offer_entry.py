"""Private card -> existing offer FSM, temporary SQLite and fake Telegram only."""
import asyncio
import sqlite3
import unittest
from types import SimpleNamespace as R
from unittest.mock import AsyncMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import test_inbot_publication as fixtures
import bot
import lead_journey as journey
import provider_registry as registry
import storage
import user_access


class CardOfferEntryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.InBotPublicationTests.asyncSetUp
    rows = fixtures.InBotPublicationTests.rows
    subscribe = fixtures.InBotPublicationTests.subscribe
    message_for = fixtures.InBotPublicationTests.message_for

    async def active(self):
        await self.subscribe()
        lid, _ = await bot.save_lead_from_draft(self.customer, self.data['phone'], self.data)
        self.assertTrue(await bot.lead_publication.activate_new(lid, 501))
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=123456, chat_id=701, user_id=701))
        return lid

    def callback(self, lid, actor=701, chat=None, action=None):
        m = self.message_for(actor)
        if chat is not None:
            m.chat = chat
        return R(data=action or f'offer_open:{lid}', message=m,
                 from_user=R(id=actor, full_name='TEST исполнитель', username='test'), answer=AsyncMock())

    async def test_card_and_notification_use_single_column_existing_offer_entry(self):
        lid = await self.active()
        lead = await bot.get_lead(lid)
        _, markup = await bot.private_contact_card(lead, 701)
        self.assertEqual([[b.text for b in row] for row in markup.inline_keyboard],
                         [['📞 Получить контакт заказчика'], ['Откликнуться']])
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, f'offer_open:{lid}')
        self.assertTrue(all(len(row) == 1 for row in markup.inline_keyboard))
        # Historical callers may pass only the delivery seed. Eligibility must
        # use the current database lead, not require new fields on that seed.
        await bot.notify_test_providers(self.telegram, {k: lead[k] for k in ('id', 'work_date', 'details')})
        sent = self.telegram.send_message.await_args.kwargs['reply_markup']
        self.assertEqual(sent, markup)
        self.assertEqual(self.rows('SELECT status FROM provider_notification_reservations'), [('confirmed',)])
        cb = self.callback(lid)
        cb.data = f'customer_contact:{lid}'
        cb.message.reply_markup = markup
        cb.message.html_text = 'TEST карточка'
        cb.message.edit_text = AsyncMock()
        await bot.reveal_customer_contact(cb)
        updated = cb.message.edit_text.await_args.kwargs['reply_markup']
        self.assertEqual(updated.inline_keyboard[1], markup.inline_keyboard[1])
        self.assertIsNotNone(updated.inline_keyboard[0][0].copy_text)
        _, reopened = await bot.private_contact_card(lead, 701)
        self.assertEqual(reopened, updated)

    async def test_button_reuses_price_conditions_phone_and_confirmation_without_duplicate_delivery(self):
        lid = await self.active()
        cb = self.callback(lid)
        await bot.open_card_offer(cb, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.price.state)
        m = cb.message
        m.text = '100 TEST'; await bot.offer_price(m, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.comment.state)
        m.text = 'TEST условия'; await bot.offer_comment(m, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.phone.state)
        m.text = '+79990000001'; m.contact = None; m.forward_origin = None
        await bot.offer_phone(m, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.review.state)
        m.message_id = (await self.state.get_data())['form_keyboard_id']
        m.reply_markup = m.answer.await_args.kwargs['reply_markup']
        cb.data = 'offer_step:send'
        await bot.offer_step_action(cb, self.state, self.telegram)
        self.assertEqual(self.rows('SELECT price,comment,phone,delivery_status FROM offers'),
                         [('100 TEST', 'TEST условия', '+79990000001', 'sent')])
        delivered = self.telegram.send_message.await_count
        await bot.offer_step_action(cb, self.state, self.telegram)
        cb.data = f'offer_open:{lid}'
        await bot.open_card_offer(cb, self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.rows('SELECT count(*) FROM offers'), [(1,)])
        self.assertEqual(self.telegram.send_message.await_count, delivered)

    async def test_repeat_button_keeps_incomplete_price_and_independent_draft(self):
        from draft_storage import draft_slot
        lid = await self.active()
        other = draft_slot(self.state, 'lead')
        await other.set_state(bot.LeadForm.place)
        await other.set_data({'marker': 'keep'})
        cb = self.callback(lid)
        await bot.open_card_offer(cb, self.state)
        cb.message.text = '120 TEST'; await bot.offer_price(cb.message, self.state)
        await bot.open_card_offer(cb, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.comment.state)
        self.assertEqual((await self.state.get_data())['price'], '120 TEST')
        self.assertEqual(await other.get_data(), {'marker': 'keep'})
        self.assertEqual(await other.get_state(), bot.LeadForm.place.state)
        self.assertEqual(self.rows('SELECT count(*) FROM offers'), [(0,)])
        self.telegram.send_message.assert_not_awaited()

    async def test_wrong_chat_owner_unregistered_blocked_and_restricted_do_not_change_drafts(self):
        lid = await self.active()
        await self.state.set_state(bot.LeadForm.place)
        await self.state.set_data({'marker': 'preserve'})
        callbacks = [self.callback(lid, actor=501), self.callback(lid, actor=999),
                     self.callback(lid, chat=R(id=-100, type='group')),
                     self.callback(lid, chat=R(id=999, type='private')),
                     self.callback(lid, action='offer_open:invalid')]
        for cb in callbacks:
            await bot.open_card_offer(cb, self.state)
        await registry.set_blocked(701, True, admin_id=1, admin_ids={1}, db_path=self.path)
        await bot.open_card_offer(self.callback(lid), self.state)
        await registry.set_blocked(701, False, admin_id=1, admin_ids={1}, db_path=self.path)
        await user_access.change(1, 701, ['offers'], 'TEST', 'card-denied', expected_revision=0, owner_id=1)
        await bot.open_card_offer(self.callback(lid), self.state)
        self.assertEqual(await self.state.get_state(), bot.LeadForm.place.state)
        self.assertEqual(await self.state.get_data(), {'marker': 'preserve'})
        for actor in (501, 701, 999):
            _, markup = await bot.private_contact_card(await bot.get_lead(lid), actor)
            self.assertEqual(len(markup.inline_keyboard), 1)
        self.assertEqual(self.rows('SELECT count(*) FROM offers'), [(0,)])
        self.telegram.send_message.assert_not_awaited()

    async def test_closed_card_and_old_confirmation_cannot_send_restoration_does_not_notify(self):
        lid = await self.active()
        cb = self.callback(lid)
        await bot.open_card_offer(cb, self.state)
        await self.state.update_data(price='100 TEST', comment='TEST', phone='+79990000001')
        await self.state.set_state(bot.OfferForm.review)
        self.assertTrue(await journey.change_lifecycle(self.path, lid, 1, admin=True, reason='TEST close'))
        before = await self.state.get_data()
        await bot.open_card_offer(cb, self.state)
        self.assertEqual(await self.state.get_data(), before)
        _, markup = await bot.private_contact_card(await bot.get_lead(lid), 701)
        self.assertEqual(len(markup.inline_keyboard), 1)
        await bot.send_offer(cb.message, cb.from_user, self.state, self.telegram)
        self.assertEqual(self.rows('SELECT count(*) FROM offers'), [(0,)])
        revision, _ = await journey.moderation_snapshot(self.path, lid)
        self.assertTrue(await journey.restore_closed(self.path, lid, 1, 'TEST restore', revision, legacy_admins={1}))
        self.assertFalse(await journey.restore_closed(self.path, lid, 1, 'TEST repeat', revision, legacy_admins={1}))
        await bot.open_card_offer(cb, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.price.state)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(self.rows('SELECT count(*) FROM provider_notification_reservations'), [(0,)])

    async def test_concurrent_confirmations_remain_one_offer_and_one_customer_delivery(self):
        lid = await self.active()
        states = [FSMContext(MemoryStorage(), self.state.key) for _ in range(2)]
        for state in states:
            await state.set_state(bot.OfferForm.review)
            await state.set_data(dict(lead_id=lid, price='100 TEST', comment='TEST', phone='+79990000001'))
        cb = self.callback(lid)
        await asyncio.gather(*(bot.send_offer(cb.message, cb.from_user, state, self.telegram) for state in states))
        self.assertEqual(self.rows('SELECT count(*) FROM offers'), [(1,)])
        calls = [c for c in self.telegram.send_message.await_args_list if c.args[0] == 501]
        self.assertEqual(len(calls), 1)

    async def test_contact_and_button_reads_preserve_schema_and_business_rows(self):
        lid = await self.active()
        def snapshot():
            with sqlite3.connect(self.path) as c:
                schema = c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
                tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                return schema, {t: c.execute('SELECT * FROM "' + t + '"').fetchall() for t in tables}
        before = snapshot()
        await bot.private_contact_card(await bot.get_lead(lid), 701)
        with patch.object(bot, 'lead_text', return_value='TEST legacy contact card'):
            _, markup = await bot.private_contact_card({'id': lid}, 701)
            self.assertEqual(markup.inline_keyboard[1][0].callback_data, f'offer_open:{lid}')
        await bot.open_card_offer(self.callback(lid), self.state)
        self.assertEqual(snapshot(), before)
        with sqlite3.connect(self.path) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchall(), [('ok',)])
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(), [])
