"""The opt-in test adapter is exercised only on temporary synthetic copies."""
import hashlib
import json
import os
import sqlite3
import unittest
from contextlib import ExitStack, contextmanager, asynccontextmanager
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

    @asynccontextmanager
    async def lab_case(self, *, installed=True):
        with self.enabled() as (scope, telegram, disk):
            features = runtime.TestFeatures(scope)
            sent, sequence = [], [1000]
            async def request(_bot, method, timeout=None):
                if method.__api_method__ == 'sendMessage':
                    sequence[0] += 1
                    response = Message(message_id=sequence[0], date=1,
                        chat={'id':method.chat_id,'type':'private'}, text=method.text,
                        reply_markup=method.reply_markup if hasattr(method.reply_markup,'inline_keyboard') else None)
                    sent.append((method,response)); return response
                if method.__api_method__ in ('editMessageReplyMarkup','answerCallbackQuery'):
                    return True
                raise AssertionError('Network operation forbidden')
            backing = bot.SQLiteDraftStorage(str(scope.database))
            with ExitStack() as stack:
                stack.enter_context(patch.object(bot.storage,'DB_PATH',str(scope.database)))
                stack.enter_context(patch.object(bot,'settings',replace(bot.settings,owner_id=1,admin_ids={1,2})))
                for router in (bot.router,bot.provider_ui.router,bot.support_ui.router,bot.admin_ui.router):
                    stack.enter_context(patch.object(router,'_parent_router',None))
                stack.enter_context(patch.object(telegram.session,'make_request',side_effect=request))
                dp=bot.create_dispatcher(backing,test_features=features if installed else None)
                async def message(text,uid=1):
                    sequence[0]+=1
                    await dp.feed_update(telegram,Update.model_validate({'update_id':sequence[0],'message':{
                        'message_id':sequence[0],'date':1,'chat':{'id':uid,'type':'private'},
                        'from':{'id':uid,'is_bot':False,'first_name':'Учебный'},'text':text}}))
                async def click(data,uid=1):
                    sequence[0]+=1
                    response=next(m for _,m in reversed(sent) if m.chat.id==uid)
                    await dp.feed_update(telegram,Update.model_validate({'update_id':sequence[0],'callback_query':{
                        'id':str(sequence[0]),'chat_instance':'test','data':data,
                        'from':{'id':uid,'is_bot':False,'first_name':'Учебный'},'message':response.model_dump()}}))
                def button(label,uid=1):
                    method=next(m for m,_ in reversed(sent) if m.chat_id==uid)
                    return next(b.callback_data for row in method.reply_markup.inline_keyboard for b in row if b.text==label)
                yield features,backing,telegram,sent,message,click,button

    async def test_lab_after_paid_and_main_menu_opens_sections_without_changing_paid_quota(self):
        async with self.lab_case() as (f,backing,telegram,sent,message,click,button):
            order=f.admin.payments.create_order(1,'accepted-iphone','sample_feature',100,3)
            f.admin.payments.confirm(f.admin.provider,f.admin.provider.event(order,'paid','accepted-event'))
            f.admin.save(1,'pay',{'id':order['id']})
            with f.store.connection() as db:
                before={n:[tuple(r) for r in db.execute('SELECT * FROM '+n)] for n in
                        ('sandbox_orders','sandbox_rights','sandbox_payment_events','sandbox_usages')}
            state=FSMContext(backing,StorageKey(bot_id=runtime.BOT_ID,chat_id=1,user_id=1))
            draft=bot.draft_slot(state,'provider_action')
            await draft.set_state(bot.provider_ui.Register.name);await draft.set_data({'keep':'independent'})
            for leave in (None,'🏠 Главное меню'):
                f.admin.save(1,'pay',{'id':order['id']})
                if leave:await message(leave)
                await message('/lab')
                self.assertEqual(f.admin.session(1)['screen'],'home')
                self.assertTrue(button('Платежи — TEST'));self.assertTrue(button('Кампании'))
            old=button('Кампании')
            await click(old);self.assertEqual(f.admin.session(1)['screen'],'campaigns')
            await message('/lab');await click(button('Платежи — TEST'))
            await click(button(order['id'][:8]+' · paid'))
            self.assertIn('paid',sent[-1][0].text)
            await message('/lab');before_session=f.admin.session(1)
            await click(old);self.assertEqual(f.admin.session(1),before_session)
            with f.store.connection() as db:
                after={n:[tuple(r) for r in db.execute('SELECT * FROM '+n)] for n in before}
                self.assertEqual(db.execute('SELECT remaining FROM sandbox_rights').fetchone()[0],3)
            self.assertEqual(before,after);self.assertEqual((await draft.get_data())['keep'],'independent')

    async def test_lab_preserves_incomplete_input_and_busy_barrier_across_reopen(self):
        async with self.lab_case() as (f,backing,telegram,sent,message,click,button):
            data={'roles':['provider'],'zones':['Москва'],'categories':['каток']}
            f.admin.save(1,'compose',data)
            await message('/lab');await message('/lab')
            self.assertTrue(button('Незавершённый ввод'))
            from sandbox_admin_ui import LocalAdmin
            reopened=LocalAdmin(SandboxStore(f.store.path,test_scope=f.scope),1)
            with f.store.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM sandbox_admin_saved_input').fetchone()[0],1)
            panel=reopened.handle(1,button('Незавершённый ввод'),2000)
            panel=reopened.handle(1,panel.buttons[0][1],2000)
            self.assertTrue(panel.expects_input)
            self.assertEqual(json.loads(reopened.session(1)['data']),data)
            with f.store.transaction() as db:db.execute('UPDATE sandbox_admin_sessions SET busy=1 WHERE actor=1')
            before=reopened.session(1)
            self.assertIn('Автоматический повтор запрещён',reopened.start(1,home=True).text)
            self.assertEqual(reopened.session(1),before)
            self.assertEqual(reopened.transport.calls,[])

    async def test_campaign_bridge_requires_enabled_runtime_current_marketing_permission_and_owner_approval(self):
        async with self.lab_case() as (f,backing,telegram,sent,message,click,button):
            cid=f.admin.campaigns.draft(1,'Учебный черновик',('provider',),('Москва',))
            await message('/lab')
            await message(bot.admin_ui.ADMIN_LABEL)
            await click('admin:marketing')
            self.assertIn('Отправка отключена',sent[-1][0].text)
            entry=button('🧪 Лабораторные кампании')
            await click(entry);self.assertEqual(f.admin.session(1)['screen'],'campaigns')
            self.assertTrue(button(cid[:8]+' · draft'))
            with sqlite3.connect(f.scope.database) as db:
                db.execute("INSERT INTO admin_role_events(target_id,role,actor_id,request_id,created_at) VALUES (2,'marketer',1,'local-marketer','fixture')")
            await message('/lab',2);await message(bot.admin_ui.ADMIN_LABEL,2);await click('admin:marketing',2)
            entry=button('🧪 Лабораторные кампании',2);await click(entry,2)
            self.assertEqual(f.admin.session(2)['screen'],'campaigns')
            with sqlite3.connect(f.scope.database) as db:
                db.execute("INSERT INTO admin_role_events(target_id,role,actor_id,request_id,created_at) VALUES (2,'none',1,'local-revoked','fixture')")
            before=f.admin.session(2)
            await click(entry,2);self.assertEqual(f.admin.session(2),before)
            await message('/start',21);await click('admin:marketing',21)
            self.assertNotIn('Лабораторные кампании',sent[-1][0].text)
            await click('lab:campaigns',21);self.assertIsNone(f.admin.session(21))
            with f.store.connection() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM sandbox_campaign_queue').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT count(*) FROM sandbox_campaign_approvals').fetchone()[0],0)
            self.assertEqual(f.admin.transport.calls,[])

    async def test_campaign_bridge_absent_when_lab_is_disabled(self):
        async with self.lab_case(installed=False) as (f,backing,telegram,sent,message,click,button):
            await message('/start');await message(bot.admin_ui.ADMIN_LABEL);await click('admin:marketing')
            self.assertIn('Отправка отключена',sent[-1][0].text)
            self.assertNotIn('lab:campaigns',str(sent[-1][0].reply_markup))
            count=len(sent);await click('lab:campaigns');self.assertEqual(len(sent),count)
            self.assertIsNone(f.admin.session(1))

    def test_flag_defaults_off_and_invalid_values_are_rejected(self):
        config = {'TEST_BOT_TOKEN':'123:TEST','TEST_ADMIN_ID':'1','TEST_CHANNEL_ID':'-100123','TESTER_IDS':'1,2'}
        self.assertFalse(render_test_start.read_test_configuration(config).feature_lab_enabled)
        self.assertTrue(render_test_start.read_test_configuration({**config,runtime.FLAG:'1'}).feature_lab_enabled)
        with self.assertRaises(IsolationError):
            render_test_start.read_test_configuration({**config,runtime.FLAG:'yes'})
