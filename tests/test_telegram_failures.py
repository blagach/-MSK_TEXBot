"""Offline Telegram failure-path tests using fake bot objects only."""
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

import storage


class TelegramFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "isolated.sqlite")
        self.patchers = [
            patch.dict(os.environ, {"BOT_TOKEN": "123456:FAKE_TEST_TOKEN", "ADMIN_IDS": "900", "LEADS_CHANNEL_ID": "-10012345"}),
            patch.object(storage, "DB_PATH", self.db_path),
        ]
        for p in self.patchers:
            p.start()
        import bot
        self.bot_module = bot
        self.patchers.append(patch.object(bot, "is_admin", return_value=True))
        self.patchers[-1].start()
        with sqlite3.connect(self.db_path) as db:
            db.execute("""CREATE TABLE leads (
                id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, customer_name TEXT,
                equipment TEXT, place TEXT, work_date TEXT, details TEXT, phone TEXT,
                status TEXT NOT NULL DEFAULT 'new', created_at TEXT
            )""")
            for i in (1, 2, 3):
                db.execute("""INSERT INTO leads VALUES (?,777,'Заказчик','каток','СЗАО','завтра','Работа','+79990000000','new','now')""", (i,))
        await storage.init_schema()

    async def asyncTearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()

    def status(self, lead_id):
        with sqlite3.connect(self.db_path) as db:
            return db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[0]

    def callback(self, lead_id):
        return SimpleNamespace(
            data=f"publish:{lead_id}",
            from_user=SimpleNamespace(id=900),
            answer=AsyncMock(),
            message=None,
        )

    async def test_rejected_telegram_send_resets_for_safe_retry(self):
        fake_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramBadRequest(method=None, message="Forbidden")))
        with patch.object(self.bot_module, "notify", new_callable=AsyncMock):
            await self.bot_module.publish_lead(self.callback(1), fake_bot)
        self.assertEqual(self.status(1), "new")

    async def test_ambiguous_network_failure_blocks_automatic_retry(self):
        fake_bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramNetworkError(method=None, message="timeout")))
        with patch.object(self.bot_module, "notify", new_callable=AsyncMock):
            await self.bot_module.publish_lead(self.callback(2), fake_bot)
        self.assertEqual(self.status(2), "publishing")
        self.assertFalse(await storage.transition(2, "new", "publishing"))

    async def test_successful_publication_is_idempotent(self):
        fake_bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(chat=SimpleNamespace(id=-10012345), message_id=44)))
        with patch.object(self.bot_module.moderation_history, "record_approval", new_callable=AsyncMock), patch.object(
            self.bot_module.lead_routing, "get", new_callable=AsyncMock, return_value=None
        ):
            await self.bot_module.publish_lead(self.callback(3), fake_bot)
            await self.bot_module.publish_lead(self.callback(3), fake_bot)
        self.assertEqual(self.status(3), "published")
        self.assertEqual(fake_bot.send_message.await_count, 1)
