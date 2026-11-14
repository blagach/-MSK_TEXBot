"""The opt-in test adapter is exercised only on temporary synthetic copies."""
import hashlib
import json
import os
import sqlite3
import unittest
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, Update

os.environ.setdefault('BOT_TOKEN', '99:TEST')
os.environ.setdefault('ADMIN_IDS', '1,2')
os.environ.setdefault('LEADS_CHANNEL_ID', '-100123')

import bot
import render_test_start
import sandbox_test_runtime as runtime
from render_test_guard import IsolationError
from sandbox_store import SandboxStore
import test_sandbox_checkout_scheduler as fixtures


class TestRuntimeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CheckoutSchedulerTests.asyncSetUp

    @contextmanager
    def enabled(self):
        disk = Path(self.temp.name)/'runtime-disk'
        disk.mkdir()
        database = disk/'bot.db'
        with sqlite3.connect(self.source) as src, sqlite3.connect(database) as dst:
            src.backup(dst)
        telegram = Bot(f'{runtime.BOT_ID}:TEST_RUNTIME_ONLY')
        self.addAsyncCleanup(telegram.session.close)
        identity = SimpleNamespace(id=runtime.BOT_ID, username=runtime.USERNAME, is_bot=True)
        with (patch.object(runtime, 'DISK', disk), patch.object(Path, 'is_mount', return_value=True),
              patch.dict(os.environ, {'RENDER_SERVICE_ID': runtime.SERVICE, runtime.FLAG: '1'})):
            scope = runtime.VerifiedTestScope(telegram, identity, 1, {1, 2, 21}, database)
            yield scope, telegram, disk

    async def test_scope_cannot_write_business_database_or_run_on_production_or_disabled_flag(self):
        with self.enabled() as (scope, telegram, disk):
            self.assertFalse((disk/'feature-lab-test.sqlite').exists())
            with patch.dict(os.environ, {runtime.FLAG: '0'}), self.assertRaises(RuntimeError):
                runtime.TestFeatures(scope)
            with patch.dict(os.environ, {'RENDER_SERVICE_ID': 'srv-production'}), self.assertRaises(RuntimeError):
                scope.ensure()
            with self.assertRaises(RuntimeError):
                SandboxStore.create(scope.database, test_scope=scope)
            with self.assertRaises(RuntimeError):
                SandboxStore.create(disk/'unapproved.sqlite')
            with self.assertRaises(PermissionError):
                scope.session(telegram, 999, SimpleNamespace(type='private', id=999))
            self.assertFalse((disk/'feature-lab-test.sqlite').exists())
            wrong = SimpleNamespace(id=runtime.BOT_ID, username='MSK_TEXBot', is_bot=True)
            with self.assertRaises(RuntimeError):
                runtime.VerifiedTestScope(telegram, wrong, 1, {1}, scope.database)

    async def test_sidecar_default_free_read_only_business_and_backup_on_reopen(self):
        with self.enabled() as (scope, telegram, disk):
            original = hashlib.sha256(scope.database.read_bytes()).hexdigest()
            features = runtime.TestFeatures(scope)
            await features.refresh_audience()
            with features.store.connection() as db:
                self.assertEqual(db.execute('SELECT consent FROM sandbox_recipients WHERE id=21').fetchone()[0], 0)
            self.assertEqual(await features.checkout.open_resource(21, 'demo-item', 2000), ('free', False))
            self.assertFalse(features.checkout.enforce_paid)
            features.admin.campaigns.consent(21, True, 1000)
            review = await features.checkout.begin(21, 'demo-pack', 'runtime-fixture')
            await features.checkout.submit(21, review['id'], review['token'])
            await features.checkout.simulate(21, review['id'], 'paid', 2000)
            with sqlite3.connect(features.store.path) as db:
                before = runtime.capture(db)
            reopened = runtime.TestFeatures(scope)
            with sqlite3.connect(reopened.store.path) as db:
                self.assertEqual(runtime.capture(db), before)
            folders = list((disk/'backups').glob('feature-lab-*'))
            self.assertEqual(len(folders), 1)
            report = json.loads((folders[0]/'report.json').read_text())
            self.assertEqual(len(report['files']), 2)
            self.assertEqual(report['files'][0]['sha256'], report['files'][1]['sha256'])
            self.assertEqual(original, hashlib.sha256(scope.database.read_bytes()).hexdigest())
            self.assertEqual(features.admin.transport.calls, [])

    async def test_integrated_entry_preserves_drafts_and_main_menu_blocks_old_checkout_buttons(self):
        with self.enabled() as (scope, telegram, disk):
            features = runtime.TestFeatures(scope)
            sent, sequence = [], [1000]
            async def request(_bot, method, timeout=None):
                if method.__api_method__ == 'sendMessage':
                    sequence[0] += 1
                    response = Message(message_id=sequence[0], date=1,
                        chat={'id':method.chat_id,'type':'private'}, text=method.text,
                        reply_markup=method.reply_markup if hasattr(method.reply_markup, 'inline_keyboard') else None)
                    sent.append((method, response))
                    return response
                if method.__api_method__ in ('editMessageReplyMarkup','answerCallbackQuery'):
                    return True
                raise AssertionError('Unexpected method in network-free runtime test')
            backing = bot.SQLiteDraftStorage(str(scope.database))
            state = FSMContext(backing, StorageKey(bot_id=runtime.BOT_ID, chat_id=21, user_id=21))
            await state.set_state(bot.support_ui.Support.compose)
            await state.set_data({'support_text':'saved','support_nonce':'fixture'})
            independent = bot.draft_slot(state, 'provider_action')
            await independent.set_state(bot.provider_ui.Register.name)
            await independent.set_data({'provider_name':'keep'})
            with ExitStack() as stack:
                stack.enter_context(patch.object(bot.storage, 'DB_PATH', str(scope.database)))
                stack.enter_context(patch.object(bot, 'settings', replace(bot.settings, owner_id=1, admin_ids={1,2})))
                for router in (bot.router, bot.provider_ui.router, bot.support_ui.router, bot.admin_ui.router):
                    stack.enter_context(patch.object(router, '_parent_router', None))
                stack.enter_context(patch.object(telegram.session, 'make_request', side_effect=request))
                dp = bot.create_dispatcher(backing, test_features=features)
                async def message(text):
                    sequence[0] += 1
                    await dp.feed_update(telegram, Update.model_validate({'update_id':sequence[0], 'message':{
                        'message_id':sequence[0],'date':1,'chat':{'id':21,'type':'private'},
                        'from':{'id':21,'is_bot':False,'first_name':'Учебный'},'text':text}}))
                await message('/checkout')
                method, old_message = sent[-1]
                old = method.reply_markup.inline_keyboard[0][0].callback_data
                self.assertIsNone(await state.get_state())
                saved = bot.draft_slot(state, 'support_action')
                self.assertEqual((await saved.get_data())['support_text'], 'saved')
                self.assertEqual((await independent.get_data())['provider_name'], 'keep')
                await message('/menu')
                from dataclasses import replace as replace_key
                cursor = FSMContext(backing, replace_key(state.key, destiny='screen_controls_v1'))
                self.assertGreaterEqual((await cursor.get_data())['through'], old_message.message_id)
                sequence[0] += 1
                await dp.feed_update(telegram, Update.model_validate({'update_id':sequence[0], 'callback_query':{
                    'id':str(sequence[0]),'chat_instance':'runtime-test','data':old,
                    'from':{'id':21,'is_bot':False,'first_name':'Учебный'},
                    'message':old_message.model_dump()}}))
                with features.store.connection() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM sandbox_checkouts').fetchone()[0], 0)
                self.assertEqual((await independent.get_data())['provider_name'], 'keep')
                await message('/start')
                self.assertEqual(sent[-1][0].text, bot.WELCOME_TEXT)

    def test_flag_defaults_off_and_invalid_values_are_rejected(self):
        config = {'TEST_BOT_TOKEN':'123:TEST','TEST_ADMIN_ID':'1','TEST_CHANNEL_ID':'-100123','TESTER_IDS':'1,2'}
        self.assertFalse(render_test_start.read_test_configuration(config).feature_lab_enabled)
        self.assertTrue(render_test_start.read_test_configuration({**config,runtime.FLAG:'1'}).feature_lab_enabled)
        with self.assertRaises(IsolationError):
            render_test_start.read_test_configuration({**config,runtime.FLAG:'yes'})
