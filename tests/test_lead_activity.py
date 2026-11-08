import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.update(BOT_TOKEN="123456:TEST", ADMIN_IDS="1,2", LEADS_CHANNEL_ID="-100123")
import aiosqlite
import bot
import storage
from unittest.mock import patch
from lead_activity import activity

class LeadActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(storage, "DB_PATH", self.tmp.name + "/activity.sqlite")
        self.patch.start()
        await bot.init_db()
        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    async def asyncTearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    async def insert(self, *, minutes=10, details="Копать", customer=501, status="new"):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute(
                """INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (customer,"экскаватор","Химки","завтра",details,"+79991234567",
                 status,(self.now-timedelta(minutes=minutes)).isoformat()),
            )
            await db.commit()

    async def check(self):
        return await activity(501,equipment=" Экскаватор ",place="химки",
                              work_date="завтра",details="копать",
                              db_path=storage.DB_PATH,now=self.now)

    async def test_duplicate_and_rate_from_history(self):
        await self.insert()
        await self.insert(minutes=20,details="Другой заказ")
        self.assertEqual(await self.check(),(2,True))

    async def test_old_and_other_customer_ignored(self):
        await self.insert(minutes=1500)
        await self.insert(customer=777)
        self.assertEqual(await self.check(),(0,False))

    async def test_rejected_ignored(self):
        await self.insert(status="rejected")
        self.assertEqual(await self.check(),(0,False))

    async def test_invalid_history_fails_closed(self):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute("""INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,created_at)
                VALUES(501,'экскаватор','Химки','завтра','Копать','+79991234567','bad')""")
            await db.commit()
        with self.assertRaises(ValueError):
            await self.check()
