"""Offline publication and recovery; synthetic SQLite and fake Telegram only."""
import asyncio
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_IDS", "1,2")
os.environ.setdefault("LEADS_CHANNEL_ID", "-100123")

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import bot
import storage
import lead_publication as policy
import provider_registry as registry
import provider_dispatch
import admin_ui
import customer_contacts


class AutomaticPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "offline.sqlite")
        p = patch.object(storage, "DB_PATH", self.path)
        p.start(); self.addCleanup(p.stop)
        await bot.init_db()
        await bot.SQLiteDraftStorage(self.path).init_schema()
        self.customer = SimpleNamespace(id=501, full_name="TEST заказчик", username="test_customer")
        self.telegram = SimpleNamespace(id=123456, send_message=AsyncMock(
            return_value=SimpleNamespace(chat=SimpleNamespace(id=bot.settings.leads_channel_id), message_id=87)))
        self.telegram._provider_notification_scope = provider_dispatch.ProductionNotificationScope(
            self.telegram.id, bot.settings.leads_channel_id)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=501, type="private"), answer=AsyncMock(),
                                       bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=501, user_id=501))
        self.data = {"draft_id": "first", "equipment": "каток", "region": "Москва", "area": "СЗАО",
                     "place": "TEST улица Лесная", "work_date": "25.12.2099 09:00", "details": "",
                     "phone": "+79990000001", "payment_method": bot.payment_preferences.ANY,
                     "form_keyboard_id": 44}

    async def submit(self, **changes):
        data = {**self.data, **changes}
        await self.state.set_state(bot.LeadForm.review)
        await self.state.set_data(data)
        await bot.submit_lead(self.message, self.customer, self.state, self.telegram, data)

    def rows(self, query, params=()):
        with sqlite3.connect(self.path) as db:
            return db.execute(query, params).fetchall()

    def channel_calls(self):
        return [c for c in self.telegram.send_message.await_args_list if c.args[0] == bot.settings.leads_channel_id]

    async def test_first_customer_publishes_without_approval_and_opens_free_contact(self):
        await registry.register_provider(701, "TEST исполнитель", "+79990000002", True, self.path)
        await registry.save_alert_preferences(701, ["каток"], ["Москва"], 0, self.path)
        await registry.register_provider(702, "TEST неподходящий", "+79990000003", True, self.path)
        await registry.save_alert_preferences(702, ["манипулятор"], ["Москва"], 0, self.path)
        with patch.object(bot, "settings", replace(bot.settings, test_notification_provider_ids=frozenset())):
            await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("published",)])
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertNotIn(self.data["phone"], self.channel_calls()[0].args[1])
        self.assertEqual([c.args[0] for c in self.telegram.send_message.await_args_list], [bot.settings.leads_channel_id, 701])
        self.assertEqual(self.rows("SELECT status FROM provider_notification_reservations"), [("confirmed",)])
        self.assertEqual(self.rows("SELECT count(*) FROM customer_approvals"), [(0,)])
        self.assertEqual(self.rows("SELECT count(*) FROM provider_equipment"), [(0,)])
        self.assertIn("№1 опубликована", self.message.answer.await_args.args[0])
        self.assertIn(("lead_auto_publish", "success"), self.rows("SELECT action,result FROM admin_action_log"))
        phone = await customer_contacts.open_contact(self.path, 701, 1)
        self.assertEqual(phone, self.data["phone"])
        self.assertEqual(await customer_contacts.open_contact(self.path, 701, 1), phone)
        self.assertEqual(self.rows("SELECT count(*) FROM customer_contact_access"), [(1,)])

    async def test_double_click_and_same_submission_after_restart_never_send_again(self):
        await asyncio.gather(*(bot.submit_lead(self.message, self.customer, self.state, self.telegram, dict(self.data)) for _ in range(6)))
        self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(1,)])
        self.assertEqual(len(self.channel_calls()), 1)
        await bot.init_db()
        await self.submit()
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM lead_publications"), [(1,)])

    async def test_distinct_drafts_cannot_concurrently_duplicate_same_request(self):
        await asyncio.gather(*(bot.submit_lead(self.message, self.customer, self.state, self.telegram,
                                              {**self.data, "draft_id": str(i)}) for i in range(5)))
        self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(1,)])
        self.assertEqual(len(self.channel_calls()), 1)

    async def test_three_per_hour_limit_is_atomic_and_keeps_fourth_draft(self):
        for i in range(4):
            await self.submit(draft_id=str(i), place=f"TEST адрес {i}")
        self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(3,)])
        self.assertEqual(len(self.channel_calls()), 3)
        self.assertEqual(await self.state.get_state(), bot.LeadForm.review.state)
        self.assertEqual((await self.state.get_data())["draft_id"], "3")
        self.assertIn("трёх заявок за час", self.message.answer.await_args.args[0])

    async def test_concurrent_distinct_requests_respect_hourly_limit(self):
        await asyncio.gather(*(bot.submit_lead(self.message, self.customer, self.state, self.telegram,
                                              {**self.data, "draft_id": str(i), "place": f"TEST адрес {i}"})
                               for i in range(6)))
        self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(3,)])
        self.assertEqual(len(self.channel_calls()), 3)

    async def test_invalid_fields_and_expired_date_do_not_create_or_send(self):
        for field, value in (("phone", "wrong"), ("place", " "), ("equipment", "unknown"), ("region", "unknown")):
            await self.submit(**{field: value})
            self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(0,)])
            self.assertEqual(await self.state.get_state(), bot.LeadForm.review.state)
        self.telegram.send_message.assert_not_awaited()
        with patch.object(bot, "show_lead_step", AsyncMock()) as show:
            await self.submit(work_date="01.01.2000 09:00")
            self.assertEqual(show.await_args.args[2], bot.LeadForm.date)
        self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(0,)])

    async def test_blocked_customer_and_restriction_after_save_cannot_publish(self):
        with patch.object(bot.user_access, "allowed", AsyncMock(return_value=False)):
            await self.submit()
        self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(0,)])
        with patch.object(bot.user_access, "allowed", AsyncMock(return_value=True)), \
             patch.object(bot.user_access, "allowed_in", AsyncMock(side_effect=[True, False])):
            await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("new",)])
        self.telegram.send_message.assert_not_awaited()

    async def test_timeout_remains_publishing_and_is_not_replayed_after_restart(self):
        self.telegram.send_message.side_effect = TelegramNetworkError(
            method=SendMessage(chat_id=-1, text="TEST"), message="timeout")
        await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("publishing",)])
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM lead_publications"), [(0,)])
        await bot.init_db()
        await self.submit()
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertIn("повтор заблокирован", self.message.answer.await_args_list[0].args[0])

    async def test_explicit_rejection_preserves_request_for_admin_without_auto_retry(self):
        self.telegram.send_message.side_effect = TelegramBadRequest(
            method=SendMessage(chat_id=-1, text="TEST"), message="rejected")
        await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("new",)])
        self.assertEqual(len(self.channel_calls()), 1)
        await self.submit()
        self.assertEqual(len(self.channel_calls()), 1)

    async def test_uncertain_provider_delivery_is_persisted_and_not_repeated(self):
        await registry.register_provider(701, "TEST исполнитель", "+79990000002", True, self.path)
        await registry.save_alert_preferences(701, ["каток"], ["Москва"], 0, self.path)
        async def send(chat_id, text, **kwargs):
            if chat_id == 701:
                raise TelegramNetworkError(method=SendMessage(chat_id=701, text="TEST"), message="timeout")
            return SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=87)
        self.telegram.send_message.side_effect = send
        with patch.object(bot, "settings", replace(bot.settings, test_notification_provider_ids=frozenset())):
            await self.submit()
            await bot.init_db()
            await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("published",)])
        self.assertEqual(self.rows("SELECT status FROM provider_notification_reservations"), [("uncertain",)])
        self.assertEqual(len([c for c in self.telegram.send_message.await_args_list if c.args[0] == 701]), 1)

    async def test_channel_429_is_not_blindly_retried_and_blocks_duplicate(self):
        self.telegram.send_message.side_effect = TelegramRetryAfter(
            method=SendMessage(chat_id=-1, text="TEST"), message="rate limited", retry_after=3)
        await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("publishing",)])
        self.assertEqual(len(self.channel_calls()), 1)
        await self.submit()
        self.assertEqual(len(self.channel_calls()), 1)

    async def test_receipt_commit_failure_never_republishes_or_sends_provider_notifications(self):
        with patch.object(storage, "finish_publication", AsyncMock(side_effect=sqlite3.OperationalError("TEST disk error"))):
            await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("publishing",)])
        await self.submit()
        self.assertEqual(len(self.channel_calls()), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM provider_notification_reservations"), [(0,)])

    async def test_pause_is_persistent_owner_only_and_stale_confirmation_cannot_reenable(self):
        with self.assertRaises(PermissionError):
            await policy.set_mode(2, 1, False, 0)
        self.assertTrue(await policy.set_mode(1, 1, False, 0))
        self.assertFalse(await policy.set_mode(1, 1, True, 0))
        await bot.init_db()
        self.assertFalse((await policy.mode())["enabled"])
        await self.submit()
        self.assertEqual(self.rows("SELECT status FROM leads"), [("new",)])
        self.assertEqual(len(self.channel_calls()), 0)
        revision = (await policy.mode())["revision"]
        self.assertTrue(await policy.set_mode(1, 1, True, revision))
        await self.submit(draft_id="next", place="TEST другой адрес")
        self.assertEqual(self.rows("SELECT status FROM leads ORDER BY id"), [("new",), ("published",)])

    async def test_old_pending_leads_are_never_automatically_published(self):
        old_id, _ = await bot.save_lead_from_draft(self.customer, self.data["phone"],
                                                  {**self.data, "draft_id": "old", "place": "TEST старая"})
        await bot.init_db()
        await self.submit()
        self.assertEqual((await bot.get_lead(old_id))["status"], "new")
        self.assertEqual(len(self.channel_calls()), 1)

    async def test_old_moderation_confirmation_only_refreshes_preview_without_publication(self):
        await self.state.set_state(bot.LeadForm.review)
        await self.state.set_data(dict(self.data))
        message = MagicMock(spec=Message)
        message.chat = SimpleNamespace(id=501, type="private")
        message.message_id = 44
        message.bot = self.message.bot
        message.answer = AsyncMock(return_value=SimpleNamespace(message_id=99))
        c = SimpleNamespace(message=message, from_user=self.customer, data="lead_review:send", answer=AsyncMock())
        await bot.lead_review_action(c, self.state, self.telegram)
        self.assertEqual(self.rows("SELECT count(*) FROM leads"), [(0,)])
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual((await self.state.get_data())["phone"], self.data["phone"])
        self.assertIn("lead_review:publish", [b.callback_data for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row])

    async def test_admin_can_close_auto_published_lead_with_reason_and_keep_saved_contact(self):
        await registry.register_provider(701, "TEST исполнитель", "+79990000002", True, self.path)
        await self.submit()
        phone = await customer_contacts.open_contact(self.path, 701, 1)
        self.assertTrue(await bot.lead_journey.change_lifecycle(self.path, 1, 1, admin=True, reason="TEST нарушение"))
        self.assertEqual((await bot.get_lead(1))["status"], "closed")
        self.assertEqual(await customer_contacts.open_contact(self.path, 701, 1), phone)
        await registry.register_provider(702, "TEST другой", "+79990000003", True, self.path)
        with self.assertRaises(PermissionError):
            await customer_contacts.open_contact(self.path, 702, 1)

    async def test_owner_pause_ui_requires_confirmation_and_rejects_non_owner(self):
        message = MagicMock(spec=Message)
        message.chat = SimpleNamespace(id=1, type="private")
        message.answer = AsyncMock()
        c = SimpleNamespace(message=message, from_user=SimpleNamespace(id=1),
                            data="admin:autopublish", answer=AsyncMock())
        await admin_ui.admin_action(c, {1, 2}, service_owner_id=1)
        self.assertIn("включена", message.answer.await_args.args[0])
        c.data = "admin:autopublish:pick:0:0"
        await admin_ui.admin_action(c, {1, 2}, service_owner_id=1)
        self.assertTrue((await policy.mode())["enabled"])
        c.data = "admin:autopublish:apply:0:0"
        await admin_ui.admin_action(c, {1, 2}, service_owner_id=1)
        self.assertFalse((await policy.mode())["enabled"])
        c.from_user.id = message.chat.id = 2
        c.data = "admin:autopublish:apply:1:0"
        await admin_ui.admin_action(c, {1, 2}, service_owner_id=1)
        self.assertFalse((await policy.mode())["enabled"])

    async def test_schema_and_old_rows_are_preserved_on_isolated_backup_restore(self):
        await self.submit()
        await registry.register_provider(701, "TEST исполнитель", "+79990000002", True, self.path)
        await registry.add_equipment(701, "каток", "TEST карточка", "Москва", "весь регион", db_path=self.path)
        await registry.save_alert_preferences(701, ["каток", "манипулятор"], ["Москва", "Московская область"], 0, self.path)
        await customer_contacts.open_contact(self.path, 701, 1)
        offer = await storage.save_offer(1, 701, "TEST исполнитель", "", "100 TEST", "", "+79990000002")
        await storage.mark_offer(offer, "unconfirmed")
        await bot.save_lead_from_draft(self.customer, self.data["phone"],
                                        {**self.data, "draft_id": "old-pending", "place": "TEST прежняя"})
        durable = FSMContext(bot.SQLiteDraftStorage(self.path), self.state.key)
        await durable.set_state(bot.LeadForm.review)
        await durable.set_data({**self.data, "draft_id": "unfinished"})
        paused = bot.draft_slot(durable, "provider_alert")
        await paused.set_state("AlertPreferences:select")
        await paused.set_data({"categories": ["каток"], "regions": ["Москва"]})
        with sqlite3.connect(self.path) as source:
            schema = source.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            tables = [r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            before = {t: source.execute('SELECT * FROM "'+t+'"').fetchall() for t in tables}
            restored_path = str(Path(self.tmp.name) / "restored.sqlite")
            with sqlite3.connect(restored_path) as target:
                source.backup(target)
        with patch.object(storage, "DB_PATH", restored_path):
            await bot.init_db()
            await bot.init_db()
            with sqlite3.connect(restored_path) as restored:
                self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(restored.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(restored.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall(), schema)
                self.assertEqual({t: restored.execute('SELECT * FROM "'+t+'"').fetchall() for t in tables}, before)
            self.assertEqual((await bot.get_lead(2))["status"], "new")
            reopened = FSMContext(bot.SQLiteDraftStorage(restored_path), self.state.key)
            self.assertEqual((await reopened.get_data())["draft_id"], "unfinished")
            self.assertEqual((await bot.draft_slot(reopened, "provider_alert").get_data())["categories"], ["каток"])
