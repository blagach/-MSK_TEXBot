import asyncio
import sqlite3
import tempfile
from pathlib import Path
import unittest
import customer_contacts as contacts

class ContactAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path=str(Path(tempfile.mkdtemp(prefix='msk-contact-tests-'))/'copy.sqlite')
        with sqlite3.connect(self.path) as db:
            db.executescript('''CREATE TABLE leads(id INTEGER PRIMARY KEY,customer_id INTEGER,status TEXT,phone TEXT);
            CREATE TABLE provider_profiles(telegram_id INTEGER PRIMARY KEY,blocked INTEGER);
            CREATE TABLE user_access_events(id INTEGER,user_id INTEGER,scopes_json TEXT,reason TEXT);
            INSERT INTO leads VALUES(10,1,'published','+79990000001');
            INSERT INTO provider_profiles VALUES(2,0); INSERT INTO provider_profiles VALUES(3,0);''')
        await contacts.init_schema(self.path)
    async def test_concurrent_repeat_only_records_once_and_survives_closure(self):
        values=await asyncio.gather(*(contacts.open_contact(self.path,2,10) for _ in range(3)))
        self.assertEqual(values,['+79990000001']*3)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM customer_contact_access').fetchone()[0],1)
            db.execute("UPDATE leads SET status='closed'")
        self.assertEqual(await contacts.open_contact(self.path,2,10),values[0])
        with self.assertRaises(PermissionError): await contacts.open_contact(self.path,3,10)
    async def test_missing_blocked_and_restricted_users_cannot_read_phone(self):
        with self.assertRaises(PermissionError): await contacts.open_contact(self.path,99,10)
        with sqlite3.connect(self.path) as db: db.execute('UPDATE provider_profiles SET blocked=1 WHERE telegram_id=2')
        with self.assertRaises(PermissionError): await contacts.open_contact(self.path,2,10)
        with sqlite3.connect(self.path) as db: db.execute("INSERT INTO user_access_events VALUES(1,3,'[\"full\"]','restriction')")
        with self.assertRaises(PermissionError): await contacts.open_contact(self.path,3,10)
    async def test_private_deep_link_delivers_contact_without_offer_form(self):
        import os
        os.environ.setdefault('BOT_TOKEN','123456:TEST')
        import bot
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from draft_storage import SQLiteDraftStorage
        from aiogram.exceptions import TelegramNetworkError,TelegramBadRequest
        message=SimpleNamespace(chat=SimpleNamespace(type='private',id=2),from_user=SimpleNamespace(id=2),
            answer=AsyncMock(return_value=SimpleNamespace(message_id=55)),bot=SimpleNamespace(edit_message_text=AsyncMock()))
        drafts=SQLiteDraftStorage(self.path)
        await drafts.init_schema()
        state=FSMContext(drafts,StorageKey(bot_id=1,chat_id=2,user_id=2))
        await state.set_state(bot.LeadForm.place)
        await state.set_data({'place':'Черновик','phone':'+79990000002'})
        with patch.object(bot.storage,'DB_PATH',self.path),patch.object(bot,'get_lead',new=AsyncMock(return_value={'id':10})),patch.object(bot,'lead_text',return_value='Заявка №10 · техника · адрес'):
            await bot.start_offer(message,state,SimpleNamespace(args='contact_10'))
            self.assertIn('Заявка №10',message.answer.await_args.args[0])
            self.assertIn('+79990000001',message.answer.await_args.args[0])
            self.assertEqual(message.answer.await_args.kwargs['reply_markup'].inline_keyboard[0][0].copy_text.text,'+79990000001')
            state=FSMContext(SQLiteDraftStorage(self.path),state.key)
            await bot.start_offer(message,state,SimpleNamespace(args='contact_10'))
            message.answer.assert_awaited_once()
            self.assertEqual(message.bot.edit_message_text.await_args.kwargs['message_id'],55)
            message.bot.edit_message_text.side_effect=TelegramBadRequest(method='editMessageText',message='Bad Request: message is not modified')
            with self.assertLogs(level='INFO') as logs:
                await bot.start_offer(message,state,SimpleNamespace(args='contact_10'))
            self.assertIn('already current',logs.output[0])
            self.assertFalse(any('WARNING' in entry for entry in logs.output))
            message.bot.edit_message_text.side_effect=TelegramBadRequest(method='editMessageText',message='secret recipient and token')
            with self.assertLogs(level='WARNING') as logs:
                await bot.start_offer(message,state,SimpleNamespace(args='contact_10'))
            self.assertIn('bad_request',logs.output[0])
            self.assertNotIn('secret',str(logs.output))
            message.bot.edit_message_text.side_effect=TelegramNetworkError(method='editMessageText',message='timeout')
            await bot.start_offer(message,state,SimpleNamespace(args='contact_10'))
            message.answer.assert_awaited_once()
        self.assertEqual(await state.get_state(),bot.LeadForm.place.state)
        self.assertEqual(await state.get_data(),{'place':'Черновик','phone':'+79990000002'})
    async def test_every_unavailable_status_and_missing_lead_denies_new_access(self):
        for status in ('new','publishing','closed','completed','cancelled','rejected','archived','unknown'):
            with self.subTest(status=status):
                with sqlite3.connect(self.path) as db: db.execute('UPDATE leads SET status=?',(status,))
                with self.assertRaises(PermissionError): await contacts.open_contact(self.path,2,10)
        with self.assertRaises(PermissionError): await contacts.open_contact(self.path,2,999)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM customer_contact_access').fetchone()[0],0)
    async def test_customer_cannot_open_own_contact_as_provider(self):
        with sqlite3.connect(self.path) as db: db.execute('INSERT INTO provider_profiles VALUES(1,0)')
        with self.assertRaises(PermissionError): await contacts.open_contact(self.path,1,10)
    async def test_inline_reveal_is_private_idempotent_and_preserves_other_buttons(self):
        import os
        os.environ.setdefault('BOT_TOKEN','123456:TEST')
        import bot
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,patch
        from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
        from aiogram.exceptions import TelegramBadRequest
        message=SimpleNamespace(chat=SimpleNamespace(type='private',id=2),text='Заявка №10',html_text='Заявка №10',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[bot.respond_button(10,private=True)],[InlineKeyboardButton(text='Назад',callback_data='back')]]),edit_text=AsyncMock(),answer=AsyncMock())
        cb=SimpleNamespace(data='customer_contact:10',message=message,from_user=SimpleNamespace(id=2),answer=AsyncMock())
        with patch.object(bot.storage,'DB_PATH',self.path):
            await bot.reveal_customer_contact(cb)
            call=message.edit_text.await_args
            self.assertEqual(call.args[0].count('+79990000001'),1)
            self.assertEqual(call.kwargs['reply_markup'].inline_keyboard[0][0].copy_text.text,'+79990000001')
            self.assertEqual(call.kwargs['reply_markup'].inline_keyboard[1][0].callback_data,'back')
            message.html_text=call.args[0]
            await bot.reveal_customer_contact(cb)
            self.assertEqual(message.edit_text.await_args.args[0].count('+79990000001'),1)
            message.edit_text.side_effect=TelegramBadRequest(method='editMessageText',message='message cannot be edited')
            await bot.reveal_customer_contact(cb)
            self.assertTrue(cb.answer.await_args.kwargs['show_alert'])
            message.answer.assert_not_awaited()
            with patch.object(bot,'lead_text',return_value='Заявка №10'):
                text,markup=await bot.private_contact_card({'id':10},2)
                self.assertIn('+79990000001',text)
                self.assertEqual(markup.inline_keyboard[0][0].copy_text.text,'+79990000001')

            cb.from_user.id=3
            message.edit_text.reset_mock()
            await bot.reveal_customer_contact(cb)
            message.edit_text.assert_not_awaited()
            cb.from_user.id=2
            with sqlite3.connect(self.path) as db:db.execute('UPDATE provider_profiles SET blocked=1 WHERE telegram_id=2')
            await bot.reveal_customer_contact(cb)
            message.edit_text.assert_not_awaited()
            self.assertNotIn('+79990000001',cb.answer.await_args.args[0])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM customer_contact_access').fetchone()[0],1)
        self.assertIsNone(await contacts.open_contact(self.path,3,10,saved_only=True))
