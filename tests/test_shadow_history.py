import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.update(BOT_TOKEN="123456:TEST", ADMIN_IDS="1,2", LEADS_CHANNEL_ID="-100123")
import aiosqlite
import bot
import storage
import moderation_history
from lead_shadow_review import evaluate

class ShadowHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(storage, "DB_PATH", self.tmp.name + "/shadow.sqlite")
        self.patch.start()
        await bot.init_db()
        self.now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
        self.fields = dict(equipment="экскаватор", region="Москва", area="СЗАО",
                           place="Химки", work_date="завтра", details="Копать")

    async def asyncTearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    async def insert(self, minutes=10, details="Копать"):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute(
                """INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,created_at)
                   VALUES(501,'экскаватор','Химки','завтра',?,'+79991234567',?)""",
                (details, (self.now-timedelta(minutes=minutes)).isoformat()),
            )
            await db.commit()

    async def review(self, **overrides):
        return await evaluate(501, db_path=storage.DB_PATH, now=self.now,
                              enabled=True, **{**self.fields, **overrides})

    async def test_approved_clean_candidate_only(self):
        await moderation_history.record_approval(501, 17, storage.DB_PATH)
        self.assertEqual((await self.review()).route, "candidate")

    async def test_unapproved_requires_manual(self):
        self.assertEqual((await self.review()).route, "manual")

    async def test_duplicate_requires_manual(self):
        await moderation_history.record_approval(501, 17, storage.DB_PATH)
        await self.insert()
        self.assertEqual((await self.review()).route, "manual")

    async def test_rate_limit_requires_manual(self):
        await moderation_history.record_approval(501, 17, storage.DB_PATH)
        for i in range(3):
            await self.insert(minutes=i+1, details="Разная работа " + str(i))
        self.assertEqual((await self.review()).route, "manual")

    async def test_disabled_kill_switch(self):
        await moderation_history.record_approval(501, 17, storage.DB_PATH)
        result = await evaluate(501, db_path=storage.DB_PATH, now=self.now,
                                enabled=False, **self.fields)
        self.assertEqual(result.route, "manual")

    async def test_missing_history_fails_closed(self):
        result = await evaluate(501, db_path=self.tmp.name + "/missing.sqlite",
                                now=self.now, enabled=True, **self.fields)
        self.assertEqual(result.route, "manual")
