"""Offline tests for failed offer delivery and duplicate suppression."""
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramForbiddenError

import storage


class OfferDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "offers.sqlite")
        self.patcher = patch.object(storage, "DB_PATH", self.path)
        self.patcher.start()
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE leads(id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
            db.execute("INSERT INTO leads VALUES(1,'published')")
        await storage.init_schema()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    async def test_failed_delivery_is_preserved_for_review(self):
        offer_id = await storage.save_offer(1, 101, "Тест", "@test", "25000", "8 часов", "+79990000000")
        self.assertIsNotNone(offer_id)
        fake_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramForbiddenError(method=None, message="blocked")))
        import bot
        delivered = await bot.notify(fake_bot, 777, "Тестовый отклик")
        self.assertFalse(delivered)
        await storage.mark_offer(offer_id, "unconfirmed")
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT delivery_status FROM offers WHERE id=?", (offer_id,)).fetchone()
            self.assertEqual(row[0], "unconfirmed")

    async def test_duplicate_offer_not_saved_twice(self):
        first = await storage.save_offer(1, 101, "Тест", "@test", "25000", "8 часов", "+79990000000")
        second = await storage.save_offer(1, 101, "Тест", "@test", "26000", "8 часов", "+79990000000")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM offers").fetchone()[0], 1)
