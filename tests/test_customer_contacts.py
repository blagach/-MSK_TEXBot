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
        message=SimpleNamespace(chat=SimpleNamespace(type='private'),from_user=SimpleNamespace(id=2),answer=AsyncMock())
        state=AsyncMock()
        with patch.object(bot.storage,'DB_PATH',self.path):
            await bot.start_offer(message,state,SimpleNamespace(args='contact_10'))
        self.assertIn('+79990000001',message.answer.await_args.args[0])
        state.set_state.assert_not_awaited()
        state.clear.assert_not_awaited()
