"""Temporary SQLite and stubbed Telegram; no external requests or live services."""
import asyncio
import json
from pathlib import Path
import sqlite3
import unittest
from contextlib import ExitStack
from types import SimpleNamespace as R
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse, parse_qs

from aiogram import Bot
from aiogram.types import Message, Update
from aiogram.methods import SendMessage, AnswerInlineQuery
import test_inbot_publication as fixtures
import bot
import admin_ui
import provider_ui
import support_ui
import payment_preferences as payments
import provider_registry as registry
import referral_storage as ref
import referral_ui as ui
import storage
import lead_publication


class ReferralTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.InBotPublicationTests.asyncSetUp
    rows = fixtures.InBotPublicationTests.rows
    message_for = fixtures.InBotPublicationTests.message_for

    async def enable(self):
        current = await ref.mode()
        await ref.set_mode(1, 1, True, current['revision'])
        self.code = await ref.link_code(501)
        return 'r1_' + self.code

    async def test_default_off_and_disabled_module_does_not_attribute(self):
        self.assertFalse((await ref.mode())['enabled'])
        self.assertIsNone(await ref.link_code(501))
        await ref.record_visit(701, 'r1_'+'A'*22, 1)
        self.assertEqual((await ref.stats())['arrived'], 0)
        m=R(answer=AsyncMock()); await ui.show_entry(m,701);m.answer.assert_not_awaited()

    async def test_first_touch_duplicates_competing_codes_and_restart(self):
        payload=await self.enable();other=await ref.link_code(502)
        results=await asyncio.gather(ref.record_visit(701,payload,1),ref.record_visit(701,'r1_'+other,2))
        self.assertEqual(sum(results),1)
        winner=self.rows('SELECT inviter_id FROM referral_attributions')[0][0]
        await ref.record_visit(701,payload,1)
        await ref.init_schema(self.path)  # reopened process, idempotent initializer
        await ref.record_visit(701,'r1_'+other,3)
        self.assertEqual(self.rows('SELECT inviter_id FROM referral_attributions'),[(winner,)])
        self.assertEqual((await ref.stats())['arrived'],1)
        self.assertEqual(await ref.link_code(501),self.code)

    async def test_invalid_self_organic_and_old_provider_are_not_new(self):
        payload=await self.enable()
        await ref.record_visit(501,payload,1)
        await ref.record_visit(702,None,2);await ref.record_visit(702,payload,3)
        await ref.record_visit(703,'r1_bad',4);await ref.record_visit(703,payload,5)
        await registry.register_provider(704,'TEST','+79990000004',True,self.path)
        await ref.record_visit(704,payload,6)
        self.assertEqual((await ref.stats())['arrived'],0)

    async def test_historical_draft_and_blocked_users_excluded(self):
        payload=await self.enable()
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO form_drafts VALUES (?,?,?)',('fsm:123:705:705:default',None,'{}'))
        await ref.record_visit(705,payload,1)
        await registry.register_provider(706,'TEST','+79990000006',True,self.path)
        with sqlite3.connect(self.path) as c:c.execute('UPDATE provider_profiles SET blocked=1 WHERE telegram_id=706')
        await ref.record_visit(706,payload,2)
        with sqlite3.connect(self.path) as c:c.execute('UPDATE provider_profiles SET telegram_id=501 WHERE telegram_id=706')
        await ref.record_visit(707,payload,3)
        self.assertEqual((await ref.stats())['arrived'],0)

    async def test_two_useful_actions_one_active_user_and_no_early_registration(self):
        payload=await self.enable();await ref.record_visit(701,payload,1)
        await registry.register_provider(701,'TEST','+79990000001',True,self.path)
        self.assertEqual((await ref.stats(501))['active'],0)
        await payments.save_provider(701,{payments.CASH},self.path)
        await payments.save_provider(701,{payments.VAT},self.path)
        await bot.save_lead_from_draft(R(id=701,full_name='TEST',username=None),self.data['phone'],self.data)
        self.assertTrue(await lead_publication.activate_new(1,701))
        self.assertFalse(await lead_publication.activate_new(1,701))
        self.assertEqual(await ref.stats(501),{'arrived':1,'active':1,'customers':1,'providers':1})
        self.assertEqual(await ref.stats(502),{'arrived':0,'active':0,'customers':0,'providers':0})
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE leads SET status='closed' WHERE id=1");c.execute("UPDATE leads SET status='published' WHERE id=1")
        self.assertEqual((await ref.stats(501))['customers'],1)

    async def test_pause_freezes_events_and_rejects_stale_or_nonowner_toggle(self):
        payload=await self.enable();await ref.record_visit(701,payload,1)
        current=await ref.mode()
        with self.assertRaises(PermissionError):await ref.set_mode(2,1,False,current['revision'])
        self.assertTrue(await ref.set_mode(1,1,False,current['revision']))
        self.assertFalse(await ref.set_mode(1,1,True,current['revision']))
        await ref.record_visit(702,payload,2)
        await registry.register_provider(701,'TEST','+79990000001',True,self.path)
        await payments.save_provider(701,{payments.CASH},self.path)
        self.assertEqual(await ref.stats(501),{'arrived':1,'active':0,'customers':0,'providers':0})

    async def test_inline_personal_sender_binding_and_no_send_message(self):
        await self.enable();code2=await ref.link_code(502)
        q=R(from_user=R(id=502,is_bot=False),query='invite',answer=AsyncMock())
        await ui.inline(q,'Msktex_test_dmitry_bot')
        args=q.answer.await_args
        self.assertTrue(args.kwargs['is_personal']);self.assertEqual(args.kwargs['cache_time'],0)
        text=args.args[0][0].input_message_content.message_text
        self.assertIn('r1_'+code2,text);self.assertNotIn(self.code,text);self.assertNotIn('501',text)
        self.assertEqual((await ref.stats())['arrived'],0)
        q.query='invite r1_'+self.code;await ui.inline(q,'Msktex_test_dmitry_bot')
        self.assertEqual(q.answer.await_args.args[0],[])

    async def test_keyboard_fallback_copy_and_mobile_single_column(self):
        await self.enable();link=ui.url('Msktex_test_dmitry_bot',self.code)
        for supported in (False,True):
            m=R(answer=AsyncMock());telegram=R(me=AsyncMock(return_value=R(supports_inline_queries=supported)))
            await ui.show(m,501,telegram,'Msktex_test_dmitry_bot')
            markup=m.answer.await_args.kwargs['reply_markup']
            self.assertTrue(all(len(row)==1 for row in markup.inline_keyboard))
            buttons=[r[0] for r in markup.inline_keyboard]
            self.assertEqual(buttons[0].text,'Отправить приглашение')
            if supported:
                chosen=buttons[0].switch_inline_query_chosen_chat
                self.assertTrue(chosen.allow_user_chats);self.assertTrue(chosen.allow_group_chats)
                self.assertFalse(chosen.allow_channel_chats);self.assertFalse(chosen.allow_bot_chats)
            fallback=next(b for b in buttons if b.url)
            params=parse_qs(urlparse(fallback.url).query)
            self.assertEqual(params['url'],[link]);self.assertEqual(params['text'],[ui.DESCRIPTION])
            self.assertEqual(next(b for b in buttons if b.copy_text).copy_text.text,link)
            self.assertIn('Моя статистика',[b.text for b in buttons])

    async def test_migration_and_backup_restore_preserve_existing_rows(self):
        await bot.save_lead_from_draft(self.customer,self.data['phone'],self.data)
        legacy=str(Path(self.tmp.name)/'old-schema.sqlite')
        with sqlite3.connect(self.path) as src,sqlite3.connect(legacy) as dst:
            names=[r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'referral_%'")]
            for name in names:
                if name != 'sqlite_sequence':
                    dst.execute(src.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0])
                rows=src.execute('SELECT * FROM '+name).fetchall()
                if rows and name != 'sqlite_sequence':
                    dst.executemany('INSERT INTO '+name+' VALUES ('+','.join('?'*len(rows[0]))+')',rows)
            old={t:dst.execute('SELECT * FROM '+t).fetchall() for t in names}
        await ref.init_schema(legacy);await ref.init_schema(legacy)
        with sqlite3.connect(legacy) as dst:
            self.assertEqual({t:dst.execute('SELECT * FROM '+t).fetchall() for t in names},old)
            self.assertEqual(dst.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0],len(names)+2)
        def snapshot():
            with sqlite3.connect(self.path) as c:
                ts=[r[0] for r in c.execute("select name from sqlite_master where type='table' and name not like 'referral_%'")]
                return {t:c.execute('select * from '+t).fetchall() for t in ts}
        before=snapshot();await ref.init_schema(self.path);await ref.init_schema(self.path)
        self.assertEqual(snapshot(),before)
        with sqlite3.connect(self.path) as c,sqlite3.connect(':memory:') as restored:
            c.backup(restored)
            self.assertEqual(restored.execute('pragma integrity_check').fetchall(),[('ok',)])
            self.assertEqual(restored.execute('pragma foreign_key_check').fetchall(),[])

    async def test_dispatch_first_touch_before_screen_controls_and_normal_start(self):
        payload=await self.enable();requests=[]
        async def transport(_bot, method, **kw):
            requests.append(method)
            if isinstance(method,SendMessage):return Message(message_id=90+len(requests),date=1,chat={'id':701,'type':'private'})
            if method.__api_method__ in ('editMessageReplyMarkup','answerCallbackQuery'):return True
            raise AssertionError(method.__api_method__)
        async with Bot('123456:TEST') as telegram:
            with ExitStack() as stack:
                for router in (bot.router,provider_ui.router,support_ui.router,admin_ui.router):
                    stack.enter_context(patch.object(router,'_parent_router',None))
                stack.enter_context(patch.object(telegram.session,'make_request',transport))
                dp=bot.create_dispatcher(bot.SQLiteDraftStorage(self.path))
                update=Update.model_validate({'update_id':900,'message':{'message_id':1,'date':1,'chat':{'id':701,'type':'private'},
                    'from':{'id':701,'is_bot':False,'first_name':'TEST'},'text':'/start '+payload,
                    'entities':[{'type':'bot_command','offset':0,'length':6}]}})
                await dp.feed_update(telegram,update)
                self.assertEqual((await ref.stats(501))['arrived'],1)
                self.assertTrue(any(isinstance(r,SendMessage) and bot.WELCOME_TEXT in r.text for r in requests))
                self.assertFalse(any('Ссылка на заявку устарела' in (getattr(r,'text',None) or '') for r in requests))

    async def test_referral_callback_rejects_foreign_private_card(self):
        await self.enable()
        m=Message(message_id=1,date=1,chat={'id':501,'type':'private'})
        cb=R(message=m,from_user=R(id=502,is_bot=False),data='ref:open',answer=AsyncMock())
        await bot.referral_action(cb,self.state)
        self.assertTrue(cb.answer.await_args.kwargs['show_alert'])

    async def test_invalid_bot_and_unknown_inline_query_get_empty_answer(self):
        await self.enable()
        for uid,is_bot,text in [(501,True,'invite'),(501,False,'anything')]:
            q=R(from_user=R(id=uid,is_bot=is_bot),query=text,answer=AsyncMock())
            await ui.inline(q,'Msktex_test_dmitry_bot')
            self.assertEqual(q.answer.await_args.args[0],[])

    async def test_referral_failure_does_not_rollback_saved_provider_methods(self):
        payload=await self.enable();await ref.record_visit(701,payload,1)
        await registry.register_provider(701,'TEST','+79990000001',True,self.path)
        with patch.object(ref,'provider_complete',side_effect=sqlite3.OperationalError('TEST injected failure')), self.assertLogs(level='ERROR') as logs:
            await payments.save_provider(701,{payments.CASH},self.path)
        self.assertIn('provider_complete',logs.output[0])
        self.assertEqual(self.rows('SELECT * FROM provider_payment_preferences'),[(701,payments.CASH)])
        self.assertEqual((await ref.stats())['active'],0)

    async def test_referral_trigger_rollback_does_not_cancel_lead_or_notifications(self):
        payload=await self.enable();await ref.record_visit(701,payload,1)
        lid,_=await bot.save_lead_from_draft(R(id=701,full_name='TEST',username=None),self.data['phone'],self.data)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TRIGGER ref_fault BEFORE UPDATE ON referral_attributions BEGIN SELECT RAISE(ROLLBACK,'TEST referral failure'); END")
        lead=await bot.get_lead(lid)
        with self.assertLogs(level='ERROR') as logs, patch.object(bot,'notify_test_providers',new_callable=AsyncMock) as notify:
            result=await bot.activate_lead_in_bot(self.telegram,lead,actor_id=701,automatic=True)
        self.assertEqual(result,'published');notify.assert_awaited_once()
        self.assertEqual(self.rows('SELECT status FROM leads WHERE id=?',(lid,)),[('published',)])
        self.assertEqual(self.rows("SELECT action FROM admin_action_log WHERE action='lead_auto_publish'"),[('lead_auto_publish',)])
        self.assertEqual((await ref.stats())['customers'],0)
        self.assertIn('first_lead',logs.output[0])

    async def test_referral_failure_does_not_strand_registration_fsm(self):
        payload=await self.enable();await ref.record_visit(701,payload,1)
        await registry.register_provider(701,'TEST','+79990000001',True,self.path)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TRIGGER ref_fault BEFORE UPDATE ON referral_attributions BEGIN SELECT RAISE(ROLLBACK,'TEST referral failure'); END")
        await self.state.set_state(provider_ui.Register.payment)
        message=self.message_for(701)
        with self.assertLogs(level='ERROR'):
            await provider_ui.finish_payment_selection(message,self.state,701,{payments.CASH})
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(message.answer.await_args.args[0],'Способы оплаты сохранены.')
        self.assertEqual(self.rows('SELECT method FROM provider_payment_preferences WHERE provider_id=701'),[(payments.CASH,)])
        self.assertEqual(self.rows('SELECT display_name FROM provider_profiles WHERE telegram_id=701'),[('TEST',)])

    async def test_optional_init_and_programming_faults_do_not_break_core(self):
        with patch.object(ref,'init_schema',side_effect=RuntimeError('TEST referral migration fault')),self.assertLogs(level='ERROR'):
            await bot.init_db()
        self.assertEqual(self.rows('PRAGMA integrity_check'),[('ok',)])
        self.assertFalse((await ref.mode())['enabled'])
        await ref.init_optional_schema(self.path)
        payload=await self.enable()
        with patch.object(ref,'record_visit',side_effect=RuntimeError('TEST attribution fault')),self.assertLogs(level='ERROR'):
            self.assertFalse(await ref.record_visit_safely(701,payload,1))
        self.assertEqual(self.rows('SELECT telegram_id FROM bot_visitors WHERE telegram_id=701'),[(701,)])
        message=self.message_for(701)
        with patch.object(ui,'available',side_effect=RuntimeError('TEST menu fault')),self.assertLogs(level='ERROR'):
            await bot.start(message,self.state)
        self.assertEqual(message.answer.await_args.args[0],bot.WELCOME_TEXT)

    async def test_off_old_deep_links_still_open_contact_and_existing_offer_form(self):
        from aiogram.filters import CommandObject
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage
        self.assertFalse((await ref.mode())['enabled'])
        lid,_=await bot.save_lead_from_draft(self.customer,self.data['phone'],self.data)
        self.assertTrue(await lead_publication.activate_new(lid,501))
        await registry.register_provider(701,'TEST','+79990000001',True,self.path)
        state=FSMContext(MemoryStorage(),StorageKey(bot_id=123456,chat_id=701,user_id=701))
        message=self.message_for(701)
        await bot.start_offer(message,state,CommandObject(command='start',args=f'contact_{lid}'))
        self.assertIn(self.data['phone'],message.answer.await_args.args[0])
        await bot.start_offer(message,state,CommandObject(command='start',args=f'lead_{lid}'))
        self.assertEqual(await state.get_state(),bot.OfferForm.price.state)
        self.assertEqual((await state.get_data())['lead_id'],lid)
        self.assertEqual((await ref.stats())['arrived'],0)

    async def test_fallback_never_requires_getme_inline_support(self):
        link=ui.url('Msktex_test_dmitry_bot','A'*22)
        markup=ui.invitation_markup(link,False)
        button=markup.inline_keyboard[0][0]
        self.assertEqual(button.text,'Отправить приглашение')
        self.assertIsNone(button.switch_inline_query_chosen_chat)
        self.assertEqual(parse_qs(urlparse(button.url).query)['url'],[link])

    async def test_previous_accepted_code_keeps_extended_schema_and_all_records(self):
        import subprocess
        import sys
        import tarfile
        from io import BytesIO
        payload=await self.enable();await ref.record_visit(701,payload,1)
        await registry.register_provider(701,'TEST','+79990000001',True,self.path)
        await payments.save_provider(701,{payments.CASH},self.path)
        await bot.save_lead_from_draft(self.customer,self.data['phone'],self.data)
        archive=subprocess.check_output(['git','archive','242e23ae31a87af5c4da57e008a94518295b0337'])
        previous=Path(self.tmp.name)/'previous-code';previous.mkdir()
        with tarfile.open(fileobj=BytesIO(archive)) as tar:
            tar.extractall(previous,filter='data')
        script='''import os,sqlite3,asyncio,socket
os.environ.update(BOT_TOKEN='123456:TEST',ADMIN_IDS='1',LEADS_CHANNEL_ID='-100123')
os.environ.pop('SERVICE_OWNER_ID',None)
def blocked(*a,**kw):raise AssertionError('Network forbidden')
socket.socket.connect=blocked
original=sqlite3.connect
def connect(path,*a,**kw):
 if '/var/data/' in str(path):raise AssertionError('Live SQLite forbidden')
 return original(path,*a,**kw)
sqlite3.connect=connect
import bot,storage
storage.DB_PATH=os.environ['REFERRAL_REHEARSAL_DB']
def state():
 with sqlite3.connect(storage.DB_PATH) as db:
  schema=db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
  rows={t:db.execute('SELECT * FROM "'+t+'"').fetchall() for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  assert db.execute('pragma integrity_check').fetchall()==[('ok',)]
  assert db.execute('pragma foreign_key_check').fetchall()==[]
  return schema,rows
before=state()
asyncio.run(bot.init_db())
asyncio.run(bot.SQLiteDraftStorage(storage.DB_PATH).init_schema())
assert before==state()
print('PREVIOUS_CODE_COMPATIBLE_ALL_ROWS_PRESERVED')
'''
        import os
        env=dict(os.environ,REFERRAL_REHEARSAL_DB=self.path)
        result=subprocess.run([sys.executable,'-B','-c',script],cwd=previous,env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('PREVIOUS_CODE_COMPATIBLE',result.stdout)

    async def test_off_inline_returns_no_stale_personal_result(self):
        await self.enable();current=await ref.mode()
        await ref.set_mode(1,1,False,current['revision'])
        q=R(from_user=R(id=501,is_bot=False),query='invite',answer=AsyncMock())
        await ui.inline(q,'Msktex_test_dmitry_bot')
        self.assertEqual(q.answer.await_args.args[0],[])

    async def test_duplicate_update_cannot_attribute_two_accounts(self):
        payload=await self.enable()
        await ref.record_visit(701,payload,88);await ref.record_visit(702,payload,88)
        self.assertEqual((await ref.stats())['arrived'],1)

    async def test_inline_channel_query_is_not_an_invitation_surface(self):
        await self.enable()
        q=R(from_user=R(id=501,is_bot=False),query='invite',chat_type='channel',answer=AsyncMock())
        await ui.inline(q,'Msktex_test_dmitry_bot')
        self.assertEqual(q.answer.await_args.args[0],[])

    async def test_dispatch_inline_answers_query_without_messages_or_attribution(self):
        await self.enable();requests=[]
        async def transport(_bot,method,**kw):
            requests.append(method)
            self.assertIsInstance(method,AnswerInlineQuery)
            return True
        async with Bot('123456:TEST') as telegram:
            with ExitStack() as stack:
                for router in (bot.router,provider_ui.router,support_ui.router,admin_ui.router):
                    stack.enter_context(patch.object(router,'_parent_router',None))
                stack.enter_context(patch.object(telegram.session,'make_request',transport))
                dp=bot.create_dispatcher(bot.SQLiteDraftStorage(self.path))
                update=Update.model_validate({'update_id':901,'inline_query':{'id':'q-test',
                    'from':{'id':501,'is_bot':False,'first_name':'TEST'},'query':'invite','offset':'','chat_type':'private'}})
                await dp.feed_update(telegram,update)
        self.assertEqual(len(requests),1)
        self.assertEqual((await ref.stats())['arrived'],0)
