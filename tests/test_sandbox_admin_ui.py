"""Local Telegram dispatcher acceptance: no network, live DB, token or polling."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiogram import Bot
from aiogram.types import Update

from sandbox_admin_ui import LocalAdmin
from sandbox_campaigns import FakeTransport
from sandbox_local_telegram import LocalPreviewSession, local_bot, local_dispatcher
from sandbox_store import SandboxStore


class LocalAdminUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'ui.sqlite'
        self.store = SandboxStore.create(self.path)
        self.transport = FakeTransport()
        self.admin = LocalAdmin(self.store, owner=1, marketers=(2,), transport=self.transport)
        self.bot = local_bot()
        self.addAsyncCleanup(self.bot.session.close)
        self.now = 1000
        self.dispatcher = local_dispatcher(self.admin, lambda: self.now)
        self.sequence = 200
        self.block = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        self.block.start()
        self.addCleanup(self.block.stop)

    def last(self, uid=1):
        return next(m for m in reversed(self.bot.session.calls)
                    if m.__api_method__ == 'sendMessage' and m.chat_id == uid and m.reply_markup)

    def button(self, label, uid=1):
        return next(b.callback_data for row in self.last(uid).reply_markup.inline_keyboard
                    for b in row if b.text == label)

    async def message(self, text, uid=1, *, reply=False, photo=False):
        self.sequence += 1
        chat = {'id': uid, 'type': 'private'}
        message = {'message_id': self.sequence, 'date': 1, 'chat': chat,
                   'from': {'id': uid, 'is_bot': False, 'first_name': 'Учебный'}}
        if photo:
            message['photo'] = [{'file_id': 'fake-photo', 'file_unique_id': 'fake-unique', 'width': 10, 'height': 10}]
            message['caption'] = text
        else:
            message['text'] = text
        if reply:
            message['reply_to_message'] = {'message_id': self.admin.session(uid)['prompt_id'],
                                         'date': 1, 'chat': chat}
        await self.dispatcher.feed_update(self.bot, Update.model_validate({'update_id': self.sequence, 'message': message}))

    async def click_data(self, data, uid=1):
        self.sequence += 1
        update = {'update_id': self.sequence, 'callback_query': {'id': str(self.sequence), 'chat_instance': 'local',
                  'from': {'id': uid, 'is_bot': False, 'first_name': 'Учебный'}, 'data': data,
                  'message': {'message_id': 100, 'date': 1, 'chat': {'id': uid, 'type': 'private'},
                              'from': {'id': 99, 'is_bot': True, 'first_name': 'Local only'}}}}
        await self.dispatcher.feed_update(self.bot, Update.model_validate(update))

    async def click(self, label, uid=1):
        await self.click_data(self.button(label, uid), uid)

    def seed_recipient(self, uid=20):
        self.admin.campaigns.recipient(uid, 'provider', ('Москва',), ('каток',))
        self.admin.campaigns.consent(uid, True, 100)

    async def create_campaign(self, *, photo=False):
        await self.message('/start')
        await self.click('Кампании')
        await self.click('Создать кампанию')
        await self.click('Исполнители')
        await self.click('Москва')
        await self.click('Указать виды техники')
        await self.message('каток', reply=True)
        await self.message('Учебная кампания', reply=True, photo=photo)
        return json.loads(self.admin.session(1)['data'])['id']

    async def preview_campaign(self):
        await self.click('Предпросмотр и расписание')
        await self.message('1970-01-01 03:33:20', reply=True)  # UTC 2000.

    def rows(self):
        with self.store.connection() as db:
            return {r[0]: db.execute('SELECT * FROM '+r[0]+' ORDER BY rowid').fetchall()
                    for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    async def test_payment_admin_confirmation_refund_replay_and_permissions(self):
        order = self.admin.payments.create_order(20, 'fixture', 'sample_feature', 100, 2, 5000)
        await self.message('/start')
        await self.click('Платежи — TEST')
        await self.click(order['id'][:8]+' · pending')
        await self.click('Тест: подтвердить оплату')
        confirm = self.button('Подтвердить тестовое действие')
        await self.click_data(confirm)
        self.assertEqual(self.admin.payments.get_order(20, order['id'])['status'], 'paid')
        before = self.rows()
        await self.click_data(confirm)
        self.assertEqual(self.rows(), before)
        await self.message('/start', uid=2)
        self.assertFalse(any('Платежи' in b.text for row in self.last(2).reply_markup.inline_keyboard for b in row))
        await self.click_data(confirm, uid=2)
        self.assertEqual(self.admin.payments.get_order(20, order['id'])['status'], 'paid')
        await self.click('Тест: полный возврат')
        await self.click('Подтвердить тестовое действие')
        self.assertIn('Статус: refunded', self.last().text)
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sandbox_rights').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT remaining FROM sandbox_rights').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sandbox_payment_refunds').fetchone()[0], 1)
        for outcome, label in (('failed', 'Тест: ошибка оплаты'), ('cancelled', 'Тест: отменить заказ')):
            another = self.admin.payments.create_order(20, outcome, 'sample_feature', 100, 1)
            await self.click('К списку платежей')
            await self.click(another['id'][:8]+' · pending')
            await self.click(label)
            await self.click('Подтвердить тестовое действие')
            self.assertEqual(self.admin.payments.get_order(20, another['id'])['status'], outcome)

    async def test_campaign_wizard_attachment_preview_approval_schedule_pause_and_report(self):
        self.seed_recipient(20); self.seed_recipient(21)
        cid = await self.create_campaign(photo=True)
        await self.click('Изменить текст')
        await self.message('Исправленный учебный текст', reply=True)
        await self.preview_campaign()
        self.assertTrue(any(m.__api_method__ == 'sendPhoto' for m in self.bot.session.calls))
        self.assertIn('Адресатов: 2', self.last().text)
        self.assertIn('01.01.1970 03:33:20 МСК', self.last().text)
        self.assertEqual(self.transport.calls, [])
        confirmation = self.button('Подтвердить кампанию')
        await self.click_data(confirmation)
        await self.click_data(confirmation)
        self.assertEqual(self.transport.calls, [])
        await self.click('Пауза')
        reopened = LocalAdmin(SandboxStore(self.path), 1, (2,), transport=self.transport)
        self.assertIsNone(reopened.campaigns.deliver_next(self.transport, 2000, campaign=cid))
        self.admin.campaigns.consent(21, False, 1500)
        await self.click('Продолжить')
        self.now = 2000
        await self.click('Один шаг подставной очереди')
        self.assertEqual([r for r, _ in self.transport.calls], [20])
        self.assertEqual(self.transport.calls[0][1]['text'], 'Исправленный учебный текст')
        self.assertEqual(len(self.transport.calls[0][1]['attachments']), 1)
        self.now = 2002
        await self.click('Один шаг подставной очереди')
        self.assertIn('excluded: 1', self.last().text)
        self.assertIn('sent: 1', self.last().text)

    async def test_stale_reply_callback_and_revoked_editor_preserve_draft_on_restart(self):
        await self.message('/start', uid=2)
        await self.click('Кампании', uid=2)
        await self.click('Создать кампанию', uid=2)
        old = self.button('Исполнители', uid=2)
        await self.click_data(old, uid=2)
        await self.click('Москва', uid=2)
        before = self.rows()
        await self.click_data(old, uid=2)
        self.assertEqual(self.rows(), before)
        reopened = LocalAdmin(SandboxStore(self.path), 1, (2,), transport=self.transport)
        panel = reopened.start(2)
        self.assertIn('виды техники', panel.text)
        self.assertEqual(json.loads(reopened.session(2)['data'])['roles'], ['provider'])
        self.admin.marketers.clear()
        before = self.rows()
        await self.click('Все виды техники', uid=2)
        self.assertEqual(self.rows(), before)

    async def test_preview_audience_change_or_revision_change_requires_new_confirmation(self):
        self.seed_recipient()
        cid = await self.create_campaign()
        await self.preview_campaign()
        self.admin.campaigns.consent(20, False, 1100)
        await self.click('Подтвердить кампанию')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sandbox_campaign_queue').fetchone()[0], 0)
        self.assertEqual(self.transport.calls, [])
        await self.click('К кампании')
        old_edit = self.button('Изменить текст')
        self.admin.campaigns.edit(1, cid, 1, 'Другая редакция')
        before = self.rows()
        await self.click_data(old_edit)
        self.assertEqual(self.rows(), before)

    async def test_interrupted_action_is_not_replayed_and_selected_queue_does_not_send_another_campaign(self):
        self.seed_recipient()
        cid = await self.create_campaign()
        await self.preview_campaign()
        await self.click('Подтвердить кампанию')
        other = self.admin.campaigns.draft(1, 'Иная кампания', ('provider',), ('Москва',))
        preview = self.admin.campaigns.preview(1, other, 1500)
        self.admin.campaigns.approve(1, other, 1, preview['approval_token'], 1500)
        self.now = 2000
        self.transport.outcomes[20] = ['crash']
        with self.assertRaises(RuntimeError):
            await self.click('Один шаг подставной очереди')
        self.assertTrue(self.admin.session(1)['busy'])
        reopened = LocalAdmin(SandboxStore(self.path), 1, transport=self.transport)
        panel = reopened.start(1)
        self.assertIn('Автоматический повтор запрещён', panel.text)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][1]['text'], 'Учебная кампания')
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM sandbox_campaign_queue WHERE campaign=?', (cid,)).fetchone()[0], 'reserved')
            self.assertEqual(db.execute('SELECT status FROM sandbox_campaign_queue WHERE campaign=?', (other,)).fetchone()[0], 'pending')

    async def test_real_session_and_live_transport_are_rejected_without_network(self):
        with self.assertRaises(TypeError):
            LocalAdmin(self.store, 1, transport=object())
        real_session_bot = Bot('99:LOCAL_INVALID_NETWORK_BOT')
        try:
            with self.assertRaises(RuntimeError):
                await self.dispatcher.feed_update(real_session_bot, Update.model_validate({'update_id': 1, 'message': {
                    'message_id': 1, 'date': 1, 'chat': {'id': 1, 'type': 'private'},
                    'from': {'id': 1, 'is_bot': False, 'first_name': 'Учебный'}, 'text': '/start'}}))
        finally:
            await real_session_bot.session.close()
        with self.assertRaises(RuntimeError):
            await self.bot.get_updates()
        self.assertIs(type(self.bot.session), LocalPreviewSession)

    async def test_text_requires_current_reply_and_old_panel_after_start_is_inactive(self):
        await self.message('/start')
        old = self.button('Кампании')
        await self.message('/start')
        before = self.rows()
        await self.click_data(old)
        self.assertEqual(before, self.rows())
        await self.click('Кампании')
        await self.click('Создать кампанию')
        await self.click('Исполнители')
        await self.click('Москва')
        await self.click('Все виды техники')
        before = self.rows()
        await self.message('Не ответ на текущую форму')
        self.assertEqual(before, self.rows())
        await self.message('Верный ответ', reply=True)
        self.assertIn('Кампания', self.last().text)

    async def test_recipient_consent_is_self_service_and_stale_opt_in_cannot_override_later_refusal(self):
        self.seed_recipient()
        await self.message('/consent', uid=20)
        old_on = self.button('Согласен получать сообщения', uid=20)
        await self.click('Отказаться от сообщений', uid=20)
        before = self.rows()
        await self.click_data(old_on, uid=20)
        await self.click_data(old_on, uid=1)
        self.assertEqual(before, self.rows())
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT consent FROM sandbox_recipients WHERE id=20').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sandbox_consents').fetchone()[0], 2)
        await self.message('/start')
        await self.click('Согласия: сводка')
        self.assertIn('Без согласия/отказались: 1', self.last().text)

    async def test_restart_after_payment_commit_and_before_ui_completion_never_repeats_confirmation(self):
        order = self.admin.payments.create_order(20, 'crash-paid', 'sample_feature', 100, 2)
        await self.message('/start')
        await self.click('Платежи — TEST')
        await self.click(order['id'][:8]+' · pending')
        await self.click('Тест: подтвердить оплату')
        old_confirm = self.button('Подтвердить тестовое действие')
        with patch.object(self.admin, 'save', side_effect=RuntimeError('fixture failure after payment commit')):
            with self.assertRaises(RuntimeError):
                await self.click_data(old_confirm)
        self.assertEqual(self.admin.payments.get_order(20, order['id'])['status'], 'paid')
        reopened = LocalAdmin(SandboxStore(self.path), 1)
        self.assertIn('Автоматический повтор запрещён', reopened.start(1).text)
        before = self.rows()
        await self.click_data(old_confirm)
        self.assertEqual(before, self.rows())
        with self.store.connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sandbox_rights').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM sandbox_payment_events').fetchone()[0], 1)
