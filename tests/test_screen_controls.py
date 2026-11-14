"""Cross-screen regressions using real aiogram requests and isolated FSM stores."""
import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from draft_storage import SQLiteDraftStorage
from screen_controls import ScreenControls


def keyboard(*actions):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=a, callback_data=a)] for a in actions])


class ScreenControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot('99:TEST')
        self.addAsyncCleanup(self.bot.session.close)
        self.controls = ScreenControls()
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=99, chat_id=12, user_id=12))
        self.requests = []
        self.sequence = 100
        self.fail_edit = False
        self.fail_send = False

        async def transport(bot, method, **kwargs):
            self.requests.append(method)
            if method.__api_method__ == 'editMessageReplyMarkup' and self.fail_edit:
                raise TelegramBadRequest(method, 'message cannot be edited')
            if method.__api_method__.startswith('send'):
                if self.fail_send:
                    raise TelegramNetworkError(method, 'unknown delivery result')
                self.sequence += 1
                return self.message(self.sequence, method.chat_id)
            return True

        patcher = patch.object(self.bot.session, 'make_request', side_effect=transport)
        patcher.start()
        self.addCleanup(patcher.stop)

    def message(self, message_id, chat_id=12):
        return Message(message_id=message_id, date=1, chat={'id': chat_id, 'type': 'private'}).as_(self.bot)

    def callback(self, message_id, action, markup=None):
        return CallbackQuery(id='callback', from_user={'id': 12, 'is_bot': False, 'first_name': 'Тест'},
                             chat_instance='test', data=action,
                             message=self.message(message_id).model_copy(update={'reply_markup': markup})).as_(self.bot)

    async def run_event(self, handler, event=None):
        return await self.controls(handler, event or self.message(self.sequence + 1),
                                   {'bot': self.bot, 'state': self.state})

    async def show(self, markup):
        async def handler(event, data):
            return await self.bot.send_message(12, 'Экран', reply_markup=markup)
        return await self.run_event(handler)

    def edits(self):
        return [r for r in self.requests if r.__api_method__ == 'editMessageReplyMarkup']

    async def test_new_screen_retires_old_help_and_admin_panels_and_rejects_delayed_click(self):
        first = await self.show(keyboard('admin:home'))
        second = await self.show(keyboard('help:resources'))
        self.assertIn(first.message_id, [r.message_id for r in self.edits()])
        handler = AsyncMock()
        for action in ('admin:role:moderator', 'paused_action:provider_action'):
            await self.run_event(handler, self.callback(first.message_id, action))
            handler.assert_not_awaited()
        await self.run_event(handler, self.callback(second.message_id, 'help:resources'))
        handler.assert_awaited_once()

    async def test_failed_visual_edit_still_blocks_stale_callback_after_restart_and_form_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'isolated.sqlite')
            store = SQLiteDraftStorage(path)
            await store.init_schema()
            self.state = FSMContext(store, self.state.key)
            await self.state.set_data({'phone': '+79990000000', 'form_keyboard_id': 50})
            await self.state.set_state('Register:phone')
            with sqlite3.connect(path) as db:
                original_rows = db.execute('SELECT * FROM form_drafts').fetchall()
                schema = db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
            first = await self.show(keyboard('registration_cancel:yes'))
            self.fail_edit = True
            await self.show(keyboard('help:resources'))
            with sqlite3.connect(path) as db:
                self.assertEqual(schema, db.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall())
                self.assertTrue(set(original_rows) <= set(db.execute('SELECT * FROM form_drafts').fetchall()))
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            await self.state.clear()
            self.controls = ScreenControls()
            self.state = FSMContext(SQLiteDraftStorage(path), self.state.key)
            handler = AsyncMock()
            await self.run_event(handler, self.callback(first.message_id, 'registration_cancel:yes'))
            handler.assert_not_awaited()
            self.assertIsNone(await self.state.get_state())

    async def test_history_links_survive_and_mixed_panel_keeps_only_durable_buttons(self):
        first = await self.show(keyboard('help:home', 'help:ticket:5:0'))
        await self.show(keyboard('admin:home'))
        edit = next(r for r in self.edits() if r.message_id == first.message_id)
        self.assertEqual([b.callback_data for row in edit.reply_markup.inline_keyboard for b in row],
                         ['help:ticket:5:0'])
        for action in ('customer_contact:5', 'offer_call:5', 'lc:open:5', 'lc:repeat:5',
                       'publish:5', 'reject:5', 'help:ticket:5:0'):
            handler = AsyncMock()
            await self.run_event(handler, self.callback(10, action))
            handler.assert_awaited_once()

    async def test_plain_validation_keeps_panel_but_text_only_state_transition_retires_it(self):
        await self.state.set_state('Register:name')
        panel = await self.show(keyboard('registration_cancel:yes'))
        self.requests.clear()
        async def validation(event, data):
            await self.bot.send_message(12, 'Введите имя')
        await self.run_event(validation)
        self.assertFalse(self.edits())
        async def next_step(event, data):
            await self.state.set_state('Register:phone')
            await self.bot.send_message(12, 'Введите телефон')
        await self.run_event(next_step)
        self.assertEqual(self.edits()[0].message_id, panel.message_id)
        handler = AsyncMock()
        await self.run_event(handler, self.callback(panel.message_id, 'registration_cancel:yes'))
        handler.assert_not_awaited()

    async def test_menu_retires_legacy_panel_and_does_not_change_independent_draft(self):
        draft = FSMContext(self.state.storage, replace(self.state.key, destiny='draft_provider_action'))
        await draft.set_state('Register:phone')
        await draft.set_data({'phone': '+79990000000'})
        await self.state.set_data({'alert_panel': 24})
        await self.show(ReplyKeyboardRemove())
        self.assertEqual([r.message_id for r in self.edits()], [24])
        self.assertEqual(await draft.get_state(), 'Register:phone')
        self.assertEqual(await draft.get_data(), {'phone': '+79990000000'})
        handler = AsyncMock()
        await self.run_event(handler, self.callback(23, 'help:new:bug'))
        handler.assert_not_awaited()

    async def test_asynchronous_notifications_and_other_chats_do_not_retire_current_screen(self):
        panel = await self.show(keyboard('help:resources'))
        self.requests.clear()
        async def handler(event, data):
            await self.bot.send_message(20, 'Подходящая заявка', reply_markup=keyboard('customer_contact:5'))
            await asyncio.create_task(self.bot.send_message(12, 'Уведомление', reply_markup=keyboard('help:home')))
        await self.run_event(handler)
        self.assertFalse(self.edits())
        callback_handler = AsyncMock()
        await self.run_event(callback_handler, self.callback(panel.message_id, 'help:resources'))
        callback_handler.assert_awaited_once()

    async def test_all_machine_cards_on_one_screen_work_until_navigation_leaves_inventory(self):
        cards = []
        async def inventory(event, data):
            for machine in (1, 2):
                cards.append(await self.bot.send_message(12, 'Техника', reply_markup=keyboard(f'machine:edit:{machine}')))
        await self.run_event(inventory)
        self.assertFalse(self.edits())
        handler = AsyncMock()
        for card in cards:
            await self.run_event(handler, self.callback(card.message_id, 'machine:edit:1'))
        self.assertEqual(handler.await_count, 2)
        await self.show(keyboard('admin:home'))
        self.assertEqual({r.message_id for r in self.edits()}, {c.message_id for c in cards})
        handler.reset_mock()
        await self.run_event(handler, self.callback(cards[0].message_id, 'machine:pause:1'))
        handler.assert_not_awaited()

    async def test_ambiguous_new_send_invalidates_old_controls_without_retrying_send(self):
        panel = await self.show(keyboard('admin:home'))
        self.fail_send = True
        with self.assertRaises(TelegramNetworkError):
            await self.show(keyboard('help:resources'))
        self.fail_send = False
        handler = AsyncMock()
        await self.run_event(handler, self.callback(panel.message_id, 'admin:role:moderator'))
        handler.assert_not_awaited()
        self.assertEqual(sum(r.__api_method__ == 'sendMessage' for r in self.requests), 2)
