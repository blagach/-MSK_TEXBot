"""Offline integration tests: customer form -> admin -> approval history.

Uses an isolated SQLite file and fake Telegram objects; never sends messages.
"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.update(BOT_TOKEN="123456:TEST", ADMIN_IDS="1,2", LEADS_CHANNEL_ID="-100123")
import aiosqlite
import bot
import storage
import moderation_history
import lead_moderation


class LeadJourneyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(storage, "DB_PATH", self.tmp.name + "/journey.sqlite")
        self.db_patch.start()
        await bot.init_db()
        self.fake_bot = SimpleNamespace(send_message=AsyncMock(
            return_value=SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=87)))
        self.state = SimpleNamespace(
            get_data=AsyncMock(return_value={
                "equipment": "каток", "region": "Москва", "area": "СЗАО",
                "place": "улица Свободы", "work_date": "завтра 09:00",
                "details": "Укатать площадку",
            }),
            clear=AsyncMock(),
        )
        self.message = SimpleNamespace(
            text="+79991234567",
            from_user=SimpleNamespace(id=501, full_name="Тестовый заказчик"),
            answer=AsyncMock(),
        )

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def callback(self, data):
        return SimpleNamespace(
            data=data, from_user=SimpleNamespace(id=1),
            answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()),
        )

    async def create_lead(self):
        await bot.submit_lead(self.message, self.message.from_user, self.state, self.fake_bot,
                              {**await self.state.get_data(), "phone": self.message.text})
        async with aiosqlite.connect(storage.DB_PATH) as db:
            row = await (await db.execute("SELECT max(id) FROM leads")).fetchone()
            return row[0]

    async def test_new_customer_approval_then_repeat_customer(self):
        self.assertFalse(await moderation_history.approved_before(501, storage.DB_PATH))
        first = await self.create_lead()
        self.assertEqual((await bot.get_lead(first))["status"], "new")
        # New customer: only admin notifications; no channel publication.
        self.assertEqual(self.fake_bot.send_message.await_count, len(bot.settings.admin_ids))
        self.assertTrue(all(call.args[0] in bot.settings.admin_ids
                            for call in self.fake_bot.send_message.await_args_list))
        self.fake_bot.send_message.reset_mock()
        await bot.publish_lead(self.callback(f"publish:{first}"), self.fake_bot)
        self.assertEqual((await bot.get_lead(first))["status"], "published")
        self.assertTrue(await moderation_history.approved_before(501, storage.DB_PATH))
        channel_call = self.fake_bot.send_message.await_args_list[0]
        self.assertEqual(channel_call.args[0], bot.settings.leads_channel_id)
        self.assertNotIn("+79991234567", channel_call.args[1])
        self.fake_bot.send_message.reset_mock()
        await bot.publish_lead(self.callback(f"publish:{first}"), self.fake_bot)
        self.fake_bot.send_message.assert_not_awaited()
        second = await self.create_lead()
        self.assertEqual((await bot.get_lead(second))["status"], "new")
        decision = lead_moderation.assess(
            previously_approved=await moderation_history.approved_before(501, storage.DB_PATH),
            category="каток", region="Москва", area="СЗАО",
            place="улица Свободы", work_date="завтра в 9",
            details="Укатать площадку",
        )
        self.assertEqual(decision.status, "eligible")
        # Eligibility alone never triggers publication.
        self.assertEqual((await bot.get_lead(second))["status"], "new")

    async def test_rejection_does_not_approve_customer(self):
        lead_id = await self.create_lead()
        self.fake_bot.send_message.reset_mock()
        await bot.reject_lead(self.callback(f"reject:{lead_id}"), self.fake_bot)
        self.assertEqual((await bot.get_lead(lead_id))["status"], "rejected")
        self.assertFalse(await moderation_history.approved_before(501, storage.DB_PATH))
        self.fake_bot.send_message.reset_mock()
        await bot.publish_lead(self.callback(f"publish:{lead_id}"), self.fake_bot)
        self.fake_bot.send_message.assert_not_awaited()
