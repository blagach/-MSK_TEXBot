"""In-bot marketplace: isolated SQLite, synthetic actors and fake API only."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as R
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault('BOT_TOKEN', '123456:TEST')
os.environ.setdefault('ADMIN_IDS', '1')
os.environ.setdefault('LEADS_CHANNEL_ID', '-100123')
from aiogram.types import Message
from aiogram.methods import SendMessage, SendPhoto, GetChat
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramNetworkError
import bot
import storage
import lead_publication as policy
import provider_registry as registry
import provider_dispatch as dispatch
import customer_contacts


class InBotPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name)/'source.sqlite')
        p = patch.object(storage, 'DB_PATH', self.path)
        p.start(); self.addCleanup(p.stop)
        p = patch.object(bot, 'settings', replace(bot.settings,
            test_notification_provider_ids=frozenset(), bot_username='MSK_TEXBot'))
        p.start(); self.addCleanup(p.stop)
        await bot.init_db()
        await bot.SQLiteDraftStorage(self.path).init_schema()
        self.telegram = R(id=123456, send_message=AsyncMock())
        self.telegram._provider_notification_scope = dispatch.ProductionNotificationScope(123456,-100123)
        self.customer = R(id=501,full_name='TEST заказчик',username=None)
        self.data = dict(draft_id='inbot-first',equipment='каток',region='Москва',area='СЗАО',
            place='TEST условная площадка',work_date='25.12.2099 09:00',details='',phone='+79990000000')
        self.state = FSMContext(MemoryStorage(),StorageKey(bot_id=123456,chat_id=501,user_id=501))
        self.message = self.message_for(501)

    def message_for(self, actor):
        m=MagicMock(spec=Message)
        m.chat=R(id=actor,type='private');m.from_user=R(id=actor,full_name='TEST',username=None)
        m.answer=AsyncMock(return_value=R(message_id=900));m.bot=R(edit_message_reply_markup=AsyncMock())
        return m

    def rows(self, sql, args=()):
        with sqlite3.connect(self.path) as c:return c.execute(sql,args).fetchall()

    async def subscribe(self, actor=701):
        await registry.register_provider(actor,'TEST исполнитель','+79990000001',True,self.path)
        await registry.save_alert_preferences(actor,['каток','манипулятор'],['Москва','Московская область'],0,self.path)

    async def submit(self, **fields):
        data={**self.data,**fields}
        await self.state.set_state(bot.LeadForm.review)
        await self.state.set_data(data)
        await bot.submit_lead(self.message,self.customer,self.state,self.telegram,data)

    async def test_full_marketplace_without_channel_or_equipment_card(self):
        await self.subscribe()
        async def private_only(chat_id,*args,**kw):
            self.assertGreater(chat_id,0,'No channel API is permitted')
            return R(message_id=45)
        self.telegram.send_message.side_effect=private_only
        await self.submit()
        self.assertEqual(self.rows('SELECT status FROM leads'),[('published',)])
        self.assertEqual(self.rows('SELECT * FROM lead_publications'),[])
        self.assertEqual(self.rows('SELECT * FROM provider_equipment'),[])
        self.assertEqual([c.args[0] for c in self.telegram.send_message.await_args_list],[701])
        provider=self.message_for(701)
        await bot.show_matching_leads(provider,701)
        self.assertIn('№1',provider.answer.await_args.args[0])
        await bot.list_customer_leads(self.message,501)
        self.assertIn('№1',str(self.message.answer.await_args.kwargs['reply_markup']))
        keyboard=await bot.provider_ui.executor_keyboard(701,self.state)
        self.assertIn(bot.AVAILABLE_LEADS,[b.text for row in keyboard.keyboard for b in row])
        self.assertEqual(await customer_contacts.open_contact(self.path,701,1),self.data['phone'])
        offer_state=FSMContext(MemoryStorage(),StorageKey(bot_id=123456,chat_id=701,user_id=701))
        await bot.open_offer(provider,offer_state,1,701)
        self.assertEqual(await offer_state.get_state(),bot.OfferForm.price.state)
        await offer_state.set_data(dict(lead_id=1,price='100 TEST',comment='ТЕСТ',phone='+79990000001'))
        await bot.send_offer(provider,provider.from_user,offer_state,self.telegram)
        self.assertEqual(self.rows('SELECT lead_id,provider_id,delivery_status FROM offers'),[(1,701,'sent')])
        self.assertTrue(all(c.args[0]>0 for c in self.telegram.send_message.await_args_list))

    async def test_pause_blocks_automatic_and_old_admin_button_but_keeps_active_access(self):
        await self.subscribe();await self.submit()
        self.assertTrue(await policy.set_mode(1,1,False,0))
        self.telegram.send_message.reset_mock()
        await self.submit(draft_id='paused',place='TEST другая площадка')
        c=R(data='publish:2',from_user=R(id=1),message=None,answer=AsyncMock())
        with patch.object(bot,'admin_lead_id',AsyncMock(return_value=2)):
            await bot.publish_lead(c,self.telegram)
        self.assertEqual(self.rows('SELECT status FROM leads ORDER BY id'),[('published',),('new',)])
        self.telegram.send_message.assert_not_awaited()
        await bot.list_customer_leads(self.message,501)
        self.assertIn('ожидает активации',str(self.message.answer.await_args.kwargs['reply_markup']))
        await bot.show_matching_leads(self.message,701)
        self.assertIn('№1',self.message.answer.await_args.args[0])
        self.assertEqual(await customer_contacts.open_contact(self.path,701,1),self.data['phone'])
        mode=await policy.mode();await policy.set_mode(1,1,True,mode['revision'])
        await bot.init_db()
        self.assertEqual(self.rows('SELECT status FROM leads WHERE id=2'),[('new',)])

    async def test_activation_pause_race_has_one_serialized_decision(self):
        lid,_=await bot.save_lead_from_draft(self.customer,self.data['phone'],self.data)
        active,paused=await asyncio.gather(policy.activate_new(lid,501),policy.set_mode(1,1,False,0))
        self.assertTrue(paused)
        self.assertEqual(self.rows('SELECT status FROM leads'),[('published' if active else 'new',)])
        self.assertEqual(self.rows("SELECT count(*) FROM admin_action_log WHERE action='lead_auto_publish'"),[(int(active),)])
        self.telegram.send_message.assert_not_awaited()

    async def test_audit_insert_failure_rolls_back_status_and_lifecycle(self):
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TRIGGER fail_activation BEFORE INSERT ON admin_action_log WHEN NEW.action='lead_auto_publish' BEGIN SELECT RAISE(ABORT,'test audit failure'); END")
        await self.submit()
        self.assertEqual(self.rows('SELECT status FROM leads'),[('new',)])
        self.assertEqual(self.rows("SELECT action FROM lead_lifecycle_events"),[('submitted',)])
        self.telegram.send_message.assert_not_awaited()

    async def test_uncertain_notification_does_not_block_inbot_access_or_replay(self):
        await self.subscribe()
        self.telegram.send_message.side_effect=TelegramNetworkError(SendMessage(chat_id=701,text='TEST'),'unknown')
        await self.submit();await bot.init_db();await self.submit()
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.assertEqual(self.rows('SELECT status FROM provider_notification_reservations'),[('uncertain',)])
        self.assertEqual(self.rows('SELECT status FROM leads'),[('published',)])
        await bot.show_matching_leads(self.message,701)
        self.assertIn('№1',self.message.answer.await_args.args[0])

    async def test_historical_pending_uncertain_and_channel_receipts_survive_copy_restart(self):
        await self.submit()
        for n,status in [(2,'new'),(3,'publishing')]:
            await bot.save_lead_from_draft(self.customer,self.data['phone'],{**self.data,'draft_id':str(n)})
            if status=='publishing':await storage.transition(n,'new',status)
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO lead_publications VALUES (1,-100123,987)')
            c.execute("INSERT INTO provider_notification_reservations VALUES (1,701,'uncertain')")
            c.execute("INSERT INTO form_drafts VALUES ('test-preserved','ProviderForm:phone','{}')")
        def state(path):
            with sqlite3.connect(path) as c:
                schema=c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall()
                tables=[n for n, in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                return schema,{t:c.execute('SELECT * FROM "'+t+'"').fetchall() for t in tables}
        before=state(self.path);restored=str(Path(self.tmp.name)/'restored.sqlite')
        with sqlite3.connect(self.path) as src,sqlite3.connect(restored) as dst:src.backup(dst)
        with patch.object(storage,'DB_PATH',restored):
            await bot.init_db();await bot.SQLiteDraftStorage(restored).init_schema()
        self.assertEqual(state(restored),before)
        self.assertEqual(state(self.path),before)
        with sqlite3.connect(restored) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchall(),[('ok',)])
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.telegram.send_message.assert_not_awaited()

    async def test_channel_write_boundary_and_private_support_exception(self):
        middleware=dispatch.PrivateDeliveryOnly(-800)
        network=AsyncMock(return_value=True)
        for api in [SendMessage(chat_id=-100123,text='TEST'),SendPhoto(chat_id=-100123,photo='fake'),SendMessage(chat_id='@zayavki_specteh_msk',text='TEST')]:
            with self.assertRaises(RuntimeError):await middleware(network,None,api)
        network.assert_not_awaited()
        await middleware(network,None,SendMessage(chat_id=701,text='TEST'))
        await middleware(network,None,SendMessage(chat_id=-800,text='TEST support'))
        await middleware(network,None,GetChat(chat_id=-100123))
        self.assertEqual(network.await_count,3)

    async def test_wrong_actor_blocked_provider_and_nonactive_lead_are_rejected(self):
        lid,_=await bot.save_lead_from_draft(self.customer,self.data['phone'],self.data)
        with self.assertRaises(bot.user_access.AccessDenied):await policy.activate_new(lid,999)
        await self.subscribe();await registry.set_blocked(701,True,admin_id=1,admin_ids={1},db_path=self.path)
        await policy.activate_new(lid,501)
        await bot.show_matching_leads(self.message,701)
        self.assertNotIn('№1',self.message.answer.await_args.args[0])
        with self.assertRaises(PermissionError):await customer_contacts.open_contact(self.path,701,1)
