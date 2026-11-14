"""Private draft navigation on an isolated persistent SQLite, never Telegram."""
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import EditMessageReplyMarkup
from aiogram.types import Message

os.environ.setdefault('BOT_TOKEN', '99:TEST')
os.environ.setdefault('ADMIN_IDS', '1,2')
os.environ.setdefault('LEADS_CHANNEL_ID', '-100123')

import admin_access
import admin_ui
import bot
import provider_registry
import provider_ui
import storage
import support_ui


class AuxiliaryMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_and_repeated_start_keep_approved_welcome_and_all_independent_drafts(self):
        from contextlib import ExitStack
        from aiogram import Bot
        from aiogram.methods import SendMessage
        from aiogram.types import Update

        approved = (
            '👋 Добро пожаловать в MSK TEXBot!\n\n'
            'Строительная спецтехника в Москве и Московской области.\n\n'
            '📋 Заказчику — разместите заявку с видом техники, местом и временем работ. '
            'Подходящие исполнители смогут связаться с вами.\n\n'
            '🚜 Исполнителю — добавьте технику, выберите регионы и подписки, чтобы получать '
            'подходящие заявки и открывать контакты заказчиков.\n\n'
            'Выберите свою роль. Её можно сменить через главное меню. Если нужна помощь — '
            'нажмите «Помощь / Поддержка».'
        )
        sent, sequence = [], 100
        actor = {'id': 20, 'is_bot': False, 'first_name': 'Учебный пользователь'}
        chat = {'id': 20, 'type': 'private'}
        _, state = self.actor(20)
        async def transport(telegram, method, **kwargs):
            nonlocal sequence
            if isinstance(method, SendMessage):
                sent.append(method)
                sequence += 1
                return Message(message_id=sequence, date=1, chat=chat)
            self.assertEqual(method.__api_method__, 'editMessageReplyMarkup')
            return True
        def update():
            nonlocal sequence
            sequence += 1
            return Update.model_validate({'update_id': sequence, 'message': {
                'message_id': sequence, 'date': 1, 'chat': chat, 'from': actor, 'text': '/start'}})

        async with Bot('99:TEST') as telegram:
            with ExitStack() as stack:
                for router in (bot.router, provider_ui.router, support_ui.router, admin_ui.router):
                    stack.enter_context(patch.object(router, '_parent_router', None))
                stack.enter_context(patch.object(telegram.session, 'make_request', side_effect=transport))
                dispatcher = bot.create_dispatcher(self.backing)
                await dispatcher.feed_update(telegram, update())
                self.assertEqual(sent[-1].text, approved)
                self.assertEqual([b.text for row in sent[-1].reply_markup.keyboard for b in row],
                                 [bot.CUSTOMER_ROLE, bot.PROVIDER_ROLE, support_ui.HELP_LABEL])
                with sqlite3.connect(self.path) as db:
                    for table in ('provider_profiles', 'provider_equipment', 'leads', 'offers'):
                        self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
                await self.seed(state)
                before = await self.draft_rows(state)
                form = {'support_kind': 'help', 'support_nonce': 'saved', 'support_text': 'Сохранённый текст'}
                await state.set_state(support_ui.Support.compose)
                await state.set_data(form)
                await provider_registry.register_provider(21, 'Учебный профиль', '+79990000000', True, self.path)
                await provider_registry.add_equipment(21, 'каток', 'Учебная машина', 'Москва', 'весь регион', self.path)
                with sqlite3.connect(self.path) as db:
                    db.execute("INSERT INTO leads(id,customer_id,customer_name,equipment,place,work_date,details,phone,status,created_at) "
                               "VALUES (11,20,'Учебный','каток','Москва','завтра','','+79990000000','published','fixture')")
                    db.execute("INSERT INTO offers(lead_id,provider_id,provider_name,username,price,comment,phone,created_at,delivery_status) "
                               "VALUES (11,21,'Учебный','','10000','','+79990000000','fixture','unconfirmed')")
                    db.execute("INSERT INTO provider_notification_reservations VALUES (11,21,'uncertain')")
                from render_test_backup import backup_database, verify_preserved_rows
                snapshot, restored = Path(self.temp.name)/'snapshot.sqlite', Path(self.temp.name)/'restored.sqlite'
                source_rows = self.all_rows()
                backup_database(Path(self.path), snapshot)
                backup_database(snapshot, restored)
                verify_preserved_rows(snapshot, restored)
                # Exercise the candidate on a restored copy, leaving source and
                # snapshot unchanged, including historical uncertain outcomes.
                with patch.object(storage, 'DB_PATH', str(restored)), patch.object(self.backing, 'db_path', str(restored)):
                    await bot.init_db()
                    with patch.object(self, 'path', str(restored)):
                        self.assertEqual(self.all_rows(), source_rows)
                    for _ in range(2):
                        await dispatcher.feed_update(telegram, update())
                        self.assertEqual(sent[-1].text, approved)
                        self.assertTrue(sent[-1].reply_markup.is_persistent)
                        self.assertIn(bot.PAUSED_ACTIONS, [b.text for row in sent[-1].reply_markup.keyboard for b in row])
                        self.assertIsNone(await state.get_state())
                        self.assertEqual(await self.draft_rows(state), before)
                        support = bot.draft_slot(state, 'support_action')
                        self.assertEqual(await support.get_state(), support_ui.Support.compose.state)
                        self.assertEqual(await support.get_data(), form)
                    with patch.object(self, 'path', str(restored)):
                        schema, copied = self.all_rows()
                    self.assertEqual(schema, source_rows[0])
                    for name, rows in source_rows[1].items():
                        if name != 'form_drafts':
                            self.assertEqual(copied[name], rows, name)
                self.assertEqual(self.all_rows(), source_rows)
                verify_preserved_rows(Path(self.path), snapshot)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'isolated.sqlite')
        for patcher in (patch.object(storage, 'DB_PATH', self.path),
                        patch.object(bot, 'settings', replace(bot.settings, admin_ids={1, 2}, owner_id=1))):
            patcher.start()
            self.addCleanup(patcher.stop)
        await bot.init_db()
        self.backing = bot.SQLiteDraftStorage(self.path)
        await self.backing.init_schema()

    def actor(self, user=1):
        message = MagicMock(spec=Message)
        message.from_user = SimpleNamespace(id=user)
        message.chat = SimpleNamespace(id=user, type='private')
        message.answer = AsyncMock(return_value=SimpleNamespace(message_id=500))
        message.edit_reply_markup = AsyncMock()
        state = FSMContext(self.backing, StorageKey(bot_id=99, chat_id=user, user_id=user))
        return message, state

    def callback(self, message, action, user=None):
        return SimpleNamespace(message=message, data=action, from_user=SimpleNamespace(
            id=message.chat.id if user is None else user), answer=AsyncMock())

    async def seed(self, state):
        cases = [
            ('provider_action', provider_ui.AddMachine.equipment_type, {'machine_group': 6, 'machine_panel': 30}),
            ('admin_action', admin_ui.ManageAdmins.target, {'admin_role': 'support'}),
            ('admin_search', admin_ui.FindUser.results,
             {'user_query': 'Учебный', 'user_kind': 'all', 'user_after': 0, 'user_nonce': 'saved'}),
            ('admin_access', admin_ui.ManageAccess.choose,
             {'access_target': 42, 'access_scopes': [], 'access_previous': [], 'access_revision': 0, 'access_nonce': 'saved'}),
            ('admin_lead_search', admin_ui.FindLead.query, {'lead_search_id': 11}),
            ('lead', bot.LeadForm.region, {'equipment': 'каток'}),
            ('offer_11', bot.OfferForm.price, {'lead_id': 11}),
        ]
        for kind, step, data in cases:
            await self.backing.checkpoint(bot.draft_slot(state, kind).key, step, data)

    def all_rows(self):
        with sqlite3.connect(self.path) as db:
            schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            rows = {n: sorted(db.execute('SELECT * FROM "'+n+'"').fetchall(), key=repr) for n in tables}
            return schema, rows

    async def draft_rows(self, state):
        return {kind: (await bot.draft_slot(state, kind).get_state(), await bot.draft_slot(state, kind).get_data())
                for kind in ('provider_action', 'admin_action', 'admin_search', 'admin_access', 'admin_lead_search', 'lead', 'offer_11')}

    async def test_five_real_drafts_grouped_and_listing_preserves_all_sqlite_rows_after_reopen(self):
        message, state = self.actor()
        await self.seed(state)
        await provider_registry.register_provider(20, 'Учебный профиль', '+79990000000', True, self.path)
        await provider_registry.add_equipment(20, 'каток', 'Учебная машина', 'Москва', 'весь регион', self.path)
        before = self.all_rows()
        keyboard = await bot.menu_with_paused_actions(state)
        labels = [b.text for row in keyboard.keyboard for b in row]
        self.assertEqual(labels.count(bot.PAUSED_ACTIONS), 1)
        self.assertFalse(any(label.startswith('▶️ Продолжить') for label in labels))
        self.assertTrue({bot.CUSTOMER_ROLE, bot.PROVIDER_ROLE, support_ui.HELP_LABEL, admin_ui.ADMIN_LABEL} <= set(labels))
        await bot.show_paused_actions(message, state)
        rows = message.answer.await_args.kwargs['reply_markup'].inline_keyboard
        actions = [b.callback_data for row in rows for b in row]
        self.assertEqual(set(actions), {f'paused_action:{k}' for k in (
            'provider_action', 'admin_action', 'admin_search', 'admin_access', 'admin_lead_search', 'home')})
        self.assertIsNone(await state.get_state())
        reopened = FSMContext(bot.SQLiteDraftStorage(self.path), state.key)
        self.assertEqual(await bot.available_paused_actions(reopened), await bot.available_paused_actions(state))
        self.assertEqual(before, self.all_rows())
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    async def test_menu_hides_admin_drafts_after_revocation_and_single_provider_alias_stays(self):
        message, state = self.actor(2)
        await self.seed(state)
        actions = dict(await bot.available_paused_actions(state))
        self.assertEqual(set(actions), {'provider_action', 'admin_search', 'admin_lead_search'})
        await admin_access.assign_role(1, 2, 'none', 'fixture-revoke', owner_id=1)
        before = self.all_rows()
        keyboard = await bot.menu_with_paused_actions(state)
        labels = [b.text for row in keyboard.keyboard for b in row]
        self.assertIn(bot.PROVIDER_ACTION_DRAFT, labels)
        self.assertNotIn(bot.PAUSED_ACTIONS, labels)
        self.assertNotIn(admin_ui.ADMIN_LABEL, labels)
        self.assertFalse(any(label in labels for label in (bot.ADMIN_ACTION_DRAFT, bot.ADMIN_SEARCH_DRAFT,
                                                          bot.ADMIN_ACCESS_DRAFT, bot.ADMIN_LEAD_SEARCH_DRAFT)))
        self.assertEqual(before, self.all_rows())
        message.text = bot.ADMIN_SEARCH_DRAFT
        await bot.resume_auxiliary_action(message, state)
        self.assertEqual(message.answer.await_args.args[0], 'Нет доступа.')
        self.assertEqual(before, self.all_rows())

    async def test_callback_restores_only_selected_draft_and_cannot_interrupt_or_repeat_active_form(self):
        message, state = self.actor()
        await self.seed(state)
        before = await self.draft_rows(state)
        # The callback message is authored by the bot; authorization must use
        # callback.from_user, not message.from_user.
        message.from_user = SimpleNamespace(id=99)
        callback = self.callback(message, 'paused_action:admin_lead_search')
        await bot.paused_action(callback, state)
        self.assertEqual(await state.get_state(), admin_ui.FindLead.query.state)
        self.assertEqual((await state.get_data())['lead_search_id'], 11)
        self.assertIsNone(await bot.draft_slot(state, 'admin_lead_search').get_state())
        after = await self.draft_rows(state)
        self.assertEqual({k: v for k, v in before.items() if k != 'admin_lead_search'},
                         {k: v for k, v in after.items() if k != 'admin_lead_search'})
        stable = self.all_rows()
        await bot.paused_action(callback, state)
        await bot.show_paused_actions(message, state)
        self.assertEqual(stable, self.all_rows())
        self.assertIn('неактуален', callback.answer.await_args.args[0])

    async def test_forged_or_revoked_selection_cannot_touch_existing_forms(self):
        message, state = self.actor(2)
        await self.seed(state)
        await bot.show_paused_actions(message, state)
        await admin_access.assign_role(1, 2, 'none', 'fixture-revoke-selection', owner_id=1)
        before = self.all_rows()
        for callback in (self.callback(message, 'paused_action:admin_search'),
                         self.callback(message, 'paused_action:provider_action', user=20),
                         self.callback(message, 'paused_action:missing')):
            await bot.paused_action(callback, state)
        self.assertEqual(before, self.all_rows())

    async def test_unknown_state_hidden_and_staff_support_resume_requires_current_permission(self):
        message, state = self.actor(20)
        unknown = bot.draft_slot(state, 'provider_action')
        await unknown.set_state('Unknown:old')
        await unknown.set_data({'saved': 'keep'})
        staff_reply = bot.draft_slot(state, 'support_action')
        await staff_reply.set_state(support_ui.Support.reply)
        await staff_reply.set_data({'support_ticket': 5})
        before = self.all_rows()
        self.assertEqual(await bot.available_paused_actions(state), [])
        for label in (bot.SUPPORT_ACTION_DRAFT, bot.PROVIDER_ACTION_DRAFT):
            message.text = label
            await bot.resume_auxiliary_action(message, state)
        self.assertEqual(before, self.all_rows())
        await staff_reply.set_state(support_ui.Support.compose)
        self.assertEqual(dict(await bot.available_paused_actions(state)), {'support_action': bot.SUPPORT_ACTION_DRAFT})

    async def test_old_training_callbacks_blocked_without_touching_active_or_paused_drafts(self):
        message, state = self.actor(20)
        await self.seed(state)
        await state.set_state(bot.LeadForm.region)
        await state.set_data({'equipment': 'каток'})
        before = self.all_rows()
        message.edit_reply_markup.side_effect = TelegramBadRequest(EditMessageReplyMarkup(chat_id=20, message_id=30),
                                                                 'cannot edit old message')
        with patch.object(support_ui, 'show_training', new_callable=AsyncMock) as training:
            for action in ('training', 'train:provider:0', 'try:customer:1:1', 'try:provider:0:0'):
                callback = self.callback(message, 'help:' + action)
                await support_ui.support_action(callback, state, SimpleNamespace(), {1, 2}, service_owner_id=1)
                self.assertIn('недоступен', callback.answer.await_args.args[0])
            training.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertEqual(message.edit_reply_markup.await_count, 4)
        self.assertEqual(before, self.all_rows())
