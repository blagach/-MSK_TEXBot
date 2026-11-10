"""PR 14 regression tests with an isolated SQLite database and fake Telegram calls."""
import os
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardRemove

os.environ.update(BOT_TOKEN="123456:TEST", ADMIN_IDS="1,2", LEADS_CHANNEL_ID="-100123")

import bot
import storage


class OfferUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = patch.object(storage, "DB_PATH", temp.name + "/offers.sqlite")
        db_path.start()
        self.addCleanup(db_path.stop)
        await bot.init_db()
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute(
                """INSERT INTO leads
                   (customer_id,equipment,place,work_date,details,phone,status,created_at)
                   VALUES (10,'кран','Москва','завтра','работа','+79991234567','published','now')"""
            )
            await db.commit()
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=20, user_id=20))
        self.telegram = SimpleNamespace(send_message=AsyncMock(), edit_message_reply_markup=AsyncMock())
        self.message = MagicMock(spec=Message)
        self.message.chat = SimpleNamespace(id=20, type="private")
        self.message.bot = self.telegram
        self.message.contact = None
        self.message.forward_origin = None
        self.message.from_user = SimpleNamespace(id=20, full_name="Исполнитель", username="test_provider")
        self.message.answer = AsyncMock(side_effect=[
            SimpleNamespace(message_id=i) for i in range(100, 220)
        ])

    def step(self, action, message_id, markup=None):
        message = MagicMock(spec=Message)
        message.message_id = message_id
        message.chat = SimpleNamespace(id=20, type="private")
        message.bot = self.telegram
        message.reply_markup = markup
        message.answer = self.message.answer
        return SimpleNamespace(message=message, from_user=self.message.from_user,
                               data=f"offer_step:{action}", answer=AsyncMock())

    async def click(self, action):
        prompt = (await self.state.get_data())["form_keyboard_id"]
        await bot.offer_step_action(self.step(action, prompt), self.state, self.telegram)

    async def saved_offers(self):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            return await (await db.execute("SELECT price, comment, phone FROM offers")).fetchall()

    async def enter_comment(self):
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_1"))
        self.assertIsInstance(self.message.answer.await_args_list[0].kwargs["reply_markup"], ReplyKeyboardRemove)
        self.message.text = "28000"
        await bot.offer_price(self.message, self.state)
        return self.message.answer.await_args.kwargs["reply_markup"]

    async def nav(self, action):
        prompt = (await self.state.get_data())["form_keyboard_id"]
        callback = self.step(action, prompt)
        callback.data = f"form_nav:{action}"
        await bot.form_navigation(callback, self.state)
        return callback

    async def recover_current_step(self):
        current = await self.state.get_state()
        data = await self.state.get_data()
        data.pop("form_keyboard_id", None)
        await bot.open_navigation(self.message, self.state)
        panel = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual([b.text for row in panel.inline_keyboard for b in row],
                         ["▶️ Продолжить анкету", bot.OFFER_BACK, bot.OFFER_HOME])
        await self.nav("continue")
        self.assertEqual(await self.state.get_state(), current)
        restored = await self.state.get_data()
        restored.pop("form_keyboard_id", None)
        self.assertEqual(restored, data)

    async def pick_lead_option(self, index):
        prompt = (await self.state.get_data())["form_keyboard_id"]
        callback = self.step(str(index), prompt)
        callback.data = f"lead_step:{index}"
        await bot.lead_choice(callback, self.state)

    async def submit_lead_review(self):
        prompt = (await self.state.get_data())["form_keyboard_id"]
        callback = self.step("send", prompt)
        callback.data = "lead_review:send"
        await bot.lead_review_action(callback, self.state, self.telegram)

    async def skip_lead_optional(self):
        prompt = (await self.state.get_data())["form_keyboard_id"]
        callback = self.step("skip", prompt)
        await bot.skip_lead_optional(callback, self.state)

    async def test_lead_recovers_all_steps_and_keeps_values_in_main_menu(self):
        await bot.lead_start(self.message, self.state)
        await self.recover_current_step()
        await self.pick_lead_option(0)
        await self.recover_current_step()
        await self.pick_lead_option(0)
        await self.recover_current_step()
        await self.pick_lead_option(0)
        await self.recover_current_step()
        await bot.open_navigation(self.message, self.state)
        await self.nav("back")
        self.assertEqual(await self.state.get_state(), bot.LeadForm.region.state)
        self.assertEqual((await self.state.get_data())["region"], "Москва")
        await self.pick_lead_option(0)
        for handler, value in (
            (bot.lead_area, "СЗАО"),
            (bot.lead_place, "улица Свободы"),
            (bot.lead_date, "завтра в 9"),
            (bot.lead_details, "Работа на смену"),
        ):
            self.message.text = value
            await handler(self.message, self.state)
            if handler in (bot.lead_place, bot.lead_date):
                await self.skip_lead_optional()
            await self.recover_current_step()
        await self.pick_lead_option(0)
        await self.recover_current_step()
        await bot.open_navigation(self.message, self.state)
        await self.nav("home")
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.message.answer.await_args.kwargs["reply_markup"], bot.main_menu())
        await bot.customer_menu(self.message, self.state)
        keyboard = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertIn(bot.LEAD_DRAFT, [row[0].text for row in keyboard.keyboard])
        self.message.text = bot.LEAD_DRAFT
        await bot.resume_form_from_menu(self.message, self.state)
        self.assertEqual(await self.state.get_state(), bot.LeadForm.phone.state)
        self.assertEqual((await self.state.get_data())["details"], "Работа на смену")
        self.assertEqual((await self.state.get_data())["payment_method"], bot.payment_preferences.CASH)
        self.message.text = "+79990000001"
        await bot.lead_phone(self.message, self.state, self.telegram)
        await self.submit_lead_review()
        async with aiosqlite.connect(storage.DB_PATH) as db:
            rows = await (await db.execute("SELECT place, work_date, details FROM leads WHERE customer_id=20")).fetchall()
        self.assertEqual(rows, [("улица Свободы", "завтра в 9", "Работа на смену")])

    async def test_offer_recovers_all_steps_and_resumes_unsubmitted_review(self):
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_1"))
        await self.recover_current_step()
        self.message.text = "28000"
        await bot.offer_price(self.message, self.state)
        await self.recover_current_step()
        await self.click("skip")
        await self.recover_current_step()
        self.assertTrue(self.message.answer.await_args.kwargs["reply_markup"].keyboard[0][0].request_contact)
        self.message.text = "+79990000001"
        await bot.offer_phone(self.message, self.state)
        await self.recover_current_step()
        await self.click("edit")
        await self.recover_current_step()
        await bot.open_navigation(self.message, self.state)
        await self.nav("back")
        self.assertEqual(await self.state.get_state(), bot.OfferForm.review.state)
        await self.click("home")
        self.assertEqual(await self.saved_offers(), [])
        await bot.lead_start(self.message, self.state)
        self.message.text = bot.OFFER_DRAFT
        await bot.resume_form_from_menu(self.message, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.review.state)
        self.assertEqual((await self.state.get_data())["phone"], "+79990000001")
        self.assertEqual(await bot.draft_slot(self.state, "lead").get_state(), bot.LeadForm.equipment.state)
        await self.click("send")
        self.assertEqual(await self.saved_offers(), [("28000", "Без комментария", "+79990000001")])

    def reopen_sqlite_state(self, user_id=20):
        return FSMContext(bot.SQLiteDraftStorage(storage.DB_PATH), StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))

    async def test_sqlite_restores_active_and_paused_drafts_for_separate_users(self):
        existing_offer = await storage.save_offer(1, 90, "Существующий", "", "5000", "", "+79990000003")
        self.state = self.reopen_sqlite_state()
        await self.state.storage.init_schema()
        await self.state.storage.init_schema()
        self.assertEqual((await bot.get_lead(1))["status"], "published")
        self.assertIsNotNone(existing_offer)
        self.assertEqual(await self.saved_offers(), [("5000", "", "+79990000003")])

        await bot.lead_start(self.message, self.state)
        await self.pick_lead_option(0)
        await self.pick_lead_option(0)
        await self.pick_lead_option(0)
        original_draft_id = (await self.state.get_data())["draft_id"]
        self.state = self.reopen_sqlite_state()
        await self.recover_current_step()
        self.assertEqual(await self.state.get_state(), bot.LeadForm.area.state)
        self.assertEqual((await self.state.get_data())["region"], "Москва")

        await bot.start_offer(self.message, self.state, CommandObject(args="lead_1"))
        self.message.text = "28000"
        await bot.offer_price(self.message, self.state)
        await self.click("skip")
        self.message.text = "+79990000001"
        await bot.offer_phone(self.message, self.state)
        await self.click("home")
        copied_path = storage.DB_PATH + ".restore-test"
        async with aiosqlite.connect(storage.DB_PATH) as source, aiosqlite.connect(copied_path) as copied:
            await source.backup(copied)
        copied_database = patch.object(storage, "DB_PATH", copied_path)
        copied_database.start()
        self.addCleanup(copied_database.stop)
        self.state = self.reopen_sqlite_state()

        another = self.reopen_sqlite_state(21)
        self.assertEqual(await another.get_data(), {})
        self.assertIsNone(await bot.draft_slot(another, "lead").get_state())
        self.assertIsNone(await bot.draft_slot(another, "offer").get_state())
        await bot.open_navigation(self.message, self.state)
        self.assertEqual(self.message.answer.await_args.kwargs["reply_markup"], bot.main_menu())
        await bot.customer_menu(self.message, self.state)
        keyboard = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertIn(bot.LEAD_DRAFT, [row[0].text for row in keyboard.keyboard])
        await bot.executor_menu(self.message, self.state)
        keyboard = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertIn(bot.OFFER_DRAFT, [row[0].text for row in keyboard.keyboard])
        await bot.resume_draft(self.message, self.state, "lead")
        self.assertEqual((await self.state.get_data())["draft_id"], original_draft_id)
        self.assertEqual(await self.state.get_state(), bot.LeadForm.area.state)
        await bot.resume_draft(self.message, self.state, "offer")
        self.assertEqual(await self.state.get_state(), bot.OfferForm.review.state)
        self.assertEqual((await self.state.get_data())["phone"], "+79990000001")
        review_id = (await self.state.get_data())["form_keyboard_id"]
        await self.click("send")
        sends = self.telegram.send_message.await_count
        self.state = self.reopen_sqlite_state()
        await bot.offer_step_action(self.step("send", review_id), self.state, self.telegram)
        self.assertEqual(self.telegram.send_message.await_count, sends)
        self.assertEqual(len(await self.saved_offers()), 2)

    async def test_sqlite_draft_submission_is_idempotent_across_restart(self):
        self.state = self.reopen_sqlite_state()
        await self.state.storage.init_schema()
        await bot.lead_start(self.message, self.state)
        await self.pick_lead_option(0)
        await self.pick_lead_option(0)
        await self.pick_lead_option(0)
        for handler, value in (
            (bot.lead_area, "СЗАО"),
            (bot.lead_place, "улица Свободы"),
            (bot.lead_date, "завтра"),
            (bot.lead_details, "Работа на смену"),
        ):
            self.message.text = value
            await handler(self.message, self.state)
            if handler in (bot.lead_place, bot.lead_date):
                await self.skip_lead_optional()
        await self.pick_lead_option(0)
        self.message.text = "+79990000001"
        data = await self.state.get_data()
        # Crash window: SQLite commit succeeded but active state has not cleared.
        lead_id, created = await bot.save_lead_from_draft(self.message.from_user, self.message.text, data)
        self.assertTrue(created)
        self.state = self.reopen_sqlite_state()
        self.assertEqual(await self.state.get_state(), bot.LeadForm.phone.state)
        await bot.lead_phone(self.message, self.state, self.telegram)
        await self.submit_lead_review()
        self.telegram.send_message.assert_not_awaited()
        self.assertIn(f"Заявка №{lead_id} уже сохранена", self.message.answer.await_args.args[0])
        async with aiosqlite.connect(storage.DB_PATH) as db:
            self.assertEqual(await (await db.execute("SELECT id FROM leads WHERE customer_id=20")).fetchall(), [(lead_id,)])
            self.assertEqual(await (await db.execute("SELECT method FROM lead_payment_preferences WHERE lead_id=?", (lead_id,))).fetchone(), (bot.payment_preferences.CASH,))
            self.assertEqual(await (await db.execute("SELECT region, area FROM lead_routing WHERE lead_id=?", (lead_id,))).fetchone(), ("Москва", "СЗАО"))
        self.assertEqual((await bot.get_lead(lead_id))["status"], "new")

    async def test_offer_cancel_requires_confirmation_and_keeps_sqlite_archive(self):
        self.state = self.reopen_sqlite_state()
        await self.state.storage.init_schema()
        existing = await storage.save_offer(1, 90, "Существующий", "", "5000", "", "+79990000003")
        await self.enter_comment()
        await self.click("skip")
        self.message.text = "+79990000001"
        await bot.offer_phone(self.message, self.state)
        review = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual([row[0].text for row in review.inline_keyboard], [
            "✅ Отправить отклик", "✏️ Изменить данные", "❌ Отменить отклик", bot.OFFER_HOME,
        ])
        await self.click("cancel")
        self.assertEqual(await self.state.get_state(), bot.OfferForm.cancel_confirmation.state)
        await self.click("back")
        self.assertEqual((await self.state.get_data())["phone"], "+79990000001")
        await self.click("cancel")
        confirm_id = (await self.state.get_data())["form_keyboard_id"]
        await self.click("cancel_confirm")
        self.state = self.reopen_sqlite_state()
        self.assertIsNone(await self.state.get_state())
        self.assertIsNone(await bot.draft_slot(self.state, "offer").get_state())
        await bot.offer_step_action(self.step("cancel_confirm", confirm_id), self.state, self.telegram)
        self.assertIsNotNone(existing)
        self.assertEqual(await self.saved_offers(), [("5000", "", "+79990000003")])
        async with aiosqlite.connect(storage.DB_PATH) as db:
            archived = await (await db.execute(
                "SELECT json_extract(data, '$.phone') FROM form_drafts "
                "WHERE storage_key LIKE '%:draft_cancelled_offer_%'"
            )).fetchall()
            self.assertEqual(archived, [("+79990000001",)])
        self.telegram.send_message.assert_not_awaited()

    async def test_existing_offer_blocks_deep_link_without_touching_other_draft(self):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute("INSERT INTO leads SELECT 11, customer_id, customer_name, equipment, place, "
                             "work_date, details, phone, status, created_at FROM leads WHERE id=1")
            await db.commit()
        await storage.save_offer(11, 20, "Исполнитель", "", "1000", "", "+79990000001")
        await bot.lead_start(self.message, self.state)
        await self.pick_lead_option(0)
        before = await self.state.get_data()
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_11"))
        self.assertIn("уже откликнулись на заявку №11", self.message.answer.await_args.args[0])
        self.assertEqual(await self.state.get_state(), bot.LeadForm.equipment_type.state)
        self.assertEqual(await self.state.get_data(), before)

    async def test_independent_offers_restore_legacy_and_survive_switch_and_restart(self):
        self.state = self.reopen_sqlite_state()
        await self.state.storage.init_schema()
        async with aiosqlite.connect(storage.DB_PATH) as db:
            for lead_id in (11, 14):
                await db.execute("INSERT INTO leads SELECT ?, customer_id, customer_name, equipment, place, "
                                 "work_date, details, phone, status, created_at FROM leads WHERE id=1", (lead_id,))
            await db.commit()
        legacy = bot.draft_slot(self.state, "offer")
        await self.state.storage.checkpoint(legacy.key, bot.OfferForm.comment, {"lead_id": 11, "price": "11000"})
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_14"))
        self.assertEqual((await self.state.get_data())["lead_id"], 14)
        self.message.text = "14000"
        await bot.offer_price(self.message, self.state)
        stale = self.step("skip", (await self.state.get_data())["form_keyboard_id"])
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_11"))
        self.assertEqual((await self.state.get_data())["price"], "11000")
        await bot.offer_step_action(stale, self.state, self.telegram)
        stale.answer.assert_awaited_once_with("Этот шаг уже неактуален.")
        await bot.show_main_menu(self.message, self.state)
        self.state = self.reopen_sqlite_state()
        self.message.text = bot.OFFER_DRAFT
        await bot.resume_form_from_menu(self.message, self.state)
        rows = self.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([r[0].callback_data for r in rows], ["offer_draft:open:14", "offer_draft:open:11"])
        other = self.reopen_sqlite_state(21)
        self.assertEqual(await bot.offer_draft_ids(other), [])
        callback = self.step("unused", 200)
        callback.data = "offer_draft:open:11"
        await bot.offer_draft_action(callback, self.state)
        await bot.cancel_unsent_offer(self.message, 20, self.state)
        self.assertEqual(await bot.offer_draft_ids(self.state), [14])
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_14"))
        self.assertEqual((await self.state.get_data())["price"], "14000")
        await self.click("skip")
        self.message.text = "+79990000001"
        await bot.offer_phone(self.message, self.state)
        review = self.step("send", (await self.state.get_data())["form_keyboard_id"])
        await bot.offer_step_action(review, self.state, self.telegram)
        await bot.offer_step_action(review, self.state, self.telegram)
        self.assertEqual(await self.saved_offers(), [("14000", "Без комментария", "+79990000001")])
        await bot.my_offers(self.message, self.state)
        card = self.message.answer.await_args.args[0]
        for value in ("№14", "14000", "Без комментария", "отправлен"):
            self.assertIn(value, card)

    async def test_sent_offer_with_stale_draft_does_not_block_new_application(self):
        self.state = self.reopen_sqlite_state()
        await self.state.storage.init_schema()
        async with aiosqlite.connect(storage.DB_PATH) as db:
            for lead_id in (11, 14):
                await db.execute("INSERT INTO leads SELECT ?, customer_id, customer_name, equipment, place, "
                                 "work_date, details, phone, status, created_at FROM leads WHERE id=1", (lead_id,))
            await db.commit()
        await storage.save_offer(11, 20, "Исполнитель", "", "11000", "", "+79990000001")
        legacy = bot.draft_slot(self.state, "offer")
        await self.state.storage.checkpoint(legacy.key, bot.OfferForm.comment, {"lead_id": 11, "price": "11000"})
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_14"))
        self.assertEqual((await self.state.get_data())["lead_id"], 14)
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_11"))
        self.assertEqual((await self.state.get_data())["lead_id"], 14)
        self.assertEqual(await legacy.get_state(), bot.OfferForm.comment.state)

    async def test_skip_description_publish_and_open_offer_from_button(self):
        await bot.lead_start(self.message, self.state)
        await self.pick_lead_option(0)
        await self.pick_lead_option(0)
        await self.pick_lead_option(0)
        for handler, value in ((bot.lead_area, "ЦАО"), (bot.lead_place, "Тверская"), (bot.lead_date, "завтра")):
            self.message.text = value
            await handler(self.message, self.state)
            if handler in (bot.lead_place, bot.lead_date):
                await self.skip_lead_optional()
        prompt = (await self.state.get_data())["form_keyboard_id"]
        skip = self.step("skip", prompt)
        skip.data = "lead_skip_details"
        await bot.skip_lead_details(skip, self.state)
        await self.pick_lead_option(0)
        self.message.text = "+79990000001"
        await bot.lead_phone(self.message, self.state, self.telegram)
        await self.submit_lead_review()
        lead_id = 2
        self.assertEqual((await bot.get_lead(lead_id))["details"], "")
        self.telegram.send_message.reset_mock()
        self.telegram.send_message.return_value = SimpleNamespace(chat=SimpleNamespace(id=-100123), message_id=201)
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=next(iter(bot.settings.admin_ids))),
            data=f"publish:{lead_id}", answer=AsyncMock(), message=None,
        )
        await bot.provider_registry.register_provider(30, "Получатель", "+79990000003", True, storage.DB_PATH)
        with (
            patch.object(bot, "settings", replace(bot.settings, test_notification_provider_ids=frozenset({30}))),
            patch.object(bot.provider_dispatch, "recipients_for_lead", new=AsyncMock(return_value=[30])),
        ):
            await bot.publish_lead(callback, self.telegram)
        self.assertEqual((await bot.get_lead(lead_id))["status"], "published")
        channel, customer_notice, notification = self.telegram.send_message.await_args_list
        self.assertIn("опубликована", customer_notice.args[1])
        self.assertIn("Детали: не указаны", channel.args[1])
        self.assertNotIn("https://", notification.args[1])
        self.assertNotIn("Описание:", notification.args[1])
        for send in (channel, notification):
            button = send.kwargs["reply_markup"].inline_keyboard[0][0]
            self.assertEqual(button.text, "🚜 Откликнуться")
            self.assertTrue(button.url.endswith("?start=lead_2"))
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_2"))
        self.assertEqual((await self.state.get_data())["lead_id"], lead_id)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.price.state)

    def keyboard_rows(self):
        markup = self.message.answer.await_args.kwargs["reply_markup"]
        return [[button.text for button in row] for row in markup.keyboard]

    async def test_compact_menus_keep_drafts_and_notification_setting_after_reopen(self):
        self.state = self.reopen_sqlite_state()
        await self.state.storage.init_schema()
        await bot.provider_registry.register_provider(20, "Исполнитель", "+79990000001", True, storage.DB_PATH)
        await bot.lead_start(self.message, self.state)
        await self.pick_lead_option(0)
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_1"))
        self.message.text = "28000"
        await bot.offer_price(self.message, self.state)
        await bot.executor_menu(self.message, self.state)
        self.assertEqual(self.keyboard_rows()[3:], [
            ["🔔 Уведомления включены", "▶️ Продолжить отклик"], ["🏠 Главное меню"],
        ])
        self.message.text = "🔔 Уведомления включены"
        await bot.provider_ui.notifications(self.message, self.state)
        self.assertEqual(self.keyboard_rows()[3], ["🔕 Уведомления выключены", "▶️ Продолжить отклик"])
        await bot.provider_ui.notifications(self.message, self.state)
        self.assertEqual(self.keyboard_rows()[3][0], "🔕 Уведомления выключены")

        self.state = self.reopen_sqlite_state()
        await bot.customer_menu(self.message, self.state)
        self.assertEqual(self.keyboard_rows(), [
            ["📝 Оставить заявку", "📋 Мои заявки"], ["▶️ Продолжить заявку"], ["🏠 Главное меню"],
        ])
        self.assertEqual(await bot.draft_slot(self.state, "lead").get_state(), bot.LeadForm.equipment_type.state)
        await bot.executor_menu(self.message, self.state)
        self.assertEqual(self.keyboard_rows()[3], ["🔕 Уведомления выключены", "▶️ Продолжить отклик"])
        self.assertEqual((await bot.draft_slot(self.state, "offer_1").get_data())["price"], "28000")
        self.message.text = "🔕 Уведомления выключены"
        await bot.provider_ui.notifications(self.message, self.state)
        self.assertEqual(self.keyboard_rows()[3][0], "🔔 Уведомления включены")
        for handler in (bot.provider_ui.my_profile, bot.provider_ui.my_machines, bot.provider_ui.my_payment_methods):
            await handler(self.message, self.state)
            if handler in (bot.lead_place, bot.lead_date):
                await self.skip_lead_optional()
            self.assertEqual(self.keyboard_rows()[3], ["🔔 Уведомления включены", "▶️ Продолжить отклик"])

    async def test_legacy_registration_remains_accessible_with_compact_menu(self):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute(
                "INSERT INTO providers VALUES (20, 'Профиль', 'кран', 'Москва', '+79990000001', 'now')"
            )
            await db.commit()
        await bot.executor_menu(self.message, self.state)
        self.assertIn([bot.provider_ui.LEGACY_PROVIDER], self.keyboard_rows())
        await bot.provider_registry.register_provider(20, "Исполнитель", "+79990000001", True, storage.DB_PATH)
        await bot.executor_menu(self.message, self.state)
        self.assertNotIn([bot.provider_ui.LEGACY_PROVIDER], self.keyboard_rows())
        await bot.provider_ui.my_profile(self.message, self.state)
        button = self.message.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, bot.provider_ui.LEGACY_PROVIDER)
        callback = self.step("legacy", 200)
        callback.data = button.callback_data
        await bot.edit_legacy_provider(callback, self.state)
        self.assertEqual(await self.state.get_state(), bot.ProviderForm.equipment.state)

    async def test_role_menus_keep_existing_provider_features_and_scope_personal_lists(self):
        self.assertEqual([[button.text for button in row] for row in bot.main_menu().keyboard],
                         [[bot.CUSTOMER_ROLE, bot.PROVIDER_ROLE], [bot.support_ui.HELP_LABEL]])
        await bot.executor_menu(self.message, self.state)
        self.assertEqual(self.keyboard_rows(), [[bot.provider_ui.REGISTER_PROVIDER], [bot.OFFER_HOME]])
        await bot.provider_registry.register_provider(20, "Исполнитель", "+79990000001", True, storage.DB_PATH)
        await bot.executor_menu(self.message, self.state)
        self.assertEqual(self.keyboard_rows(), [
            ["🔎 Подходящие заявки", "📋 Мои отклики"],
            ["👤 Мой профиль", "🚜 Моя техника"],
            ["➕ Добавить технику", "💳 Способ оплаты"],
            ["🔔 Уведомления включены"],
            ["🏠 Главное меню"],
        ])
        await bot.customer_menu(self.message, self.state)
        self.assertEqual(self.keyboard_rows(), [["📝 Оставить заявку", "📋 Мои заявки"], ["🏠 Главное меню"]])
        await bot.my_leads(self.message, self.state)
        self.assertIn("пока нет", self.message.answer.await_args.args[0])
        await storage.save_offer(1, 90, "Другой", "", "Скрытая цена", "", "+79990000003")
        await storage.save_offer(1, 20, "Исполнитель", "", "28000", "", "+79990000001")
        await bot.my_offers(self.message, self.state)
        self.assertIn("28000", self.message.answer.await_args.args[0])
        self.assertNotIn("Скрытая цена", self.message.answer.await_args.args[0])

    async def test_keyboard_replacement_back_and_home(self):
        comment_markup = await self.enter_comment()
        await self.click("skip")
        self.assertEqual(await self.state.get_state(), bot.OfferForm.phone.state)
        self.assertEqual((await self.state.get_data())["comment"], "Без комментария")
        phone_markup = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertTrue(phone_markup.keyboard[0][0].request_contact)
        self.assertEqual([button.text for button in phone_markup.keyboard[1]],
                         [bot.OFFER_BACK, bot.OFFER_HOME])
        stale = self.step("back", 102, comment_markup)
        await bot.offer_step_action(stale, self.state, self.telegram)
        stale.answer.assert_awaited_once_with("Этот шаг уже неактуален.")
        self.assertEqual(await self.state.get_state(), bot.OfferForm.phone.state)
        await bot.offer_phone_back(self.message, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.comment.state)
        self.assertIsInstance(self.message.answer.await_args_list[-2].kwargs["reply_markup"], ReplyKeyboardRemove)
        await self.click("home")
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.assertEqual(self.message.answer.await_args.kwargs["reply_markup"], bot.main_menu())
        self.assertEqual(await bot.draft_slot(self.state, "offer_1").get_state(), bot.OfferForm.comment.state)
        self.assertEqual(await self.saved_offers(), [])

    async def test_phone_validation_and_own_contact(self):
        await self.enter_comment()
        await self.click("skip")
        for text in ("+7912345678", "891234567890", "+491234567890", "79123456789", "+7abcdef0123"):
            self.message.text = text
            await bot.offer_phone(self.message, self.state)
            self.assertIn("повторите ввод", self.message.answer.await_args.args[0])
            self.assertEqual(await self.state.get_state(), bot.OfferForm.phone.state)
        for user_id, forwarded in ((99, False), (None, False), (20, True)):
            self.message.contact = SimpleNamespace(user_id=user_id, phone_number="79990000001")
            self.message.forward_origin = object() if forwarded else None
            await bot.offer_phone(self.message, self.state)
            self.assertIn("только свой номер", self.message.answer.await_args.args[0])
            self.assertEqual(await self.state.get_state(), bot.OfferForm.phone.state)
        self.message.contact = SimpleNamespace(user_id=20, phone_number="79990000001")
        self.message.forward_origin = None
        await bot.offer_phone(self.message, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.review.state)
        self.assertIn("+79990000001", self.message.answer.await_args.args[0])
        self.assertEqual(await self.saved_offers(), [])
        self.telegram.send_message.assert_not_awaited()

    async def test_edit_review_and_confirm_only_latest_values(self):
        await self.enter_comment()
        await self.click("skip")
        self.message.text = "+79990000001"
        await bot.offer_phone(self.message, self.state)
        old_review = (await self.state.get_data())["form_keyboard_id"]
        await self.click("edit")
        await self.click("price")
        self.message.text = "30000"
        await bot.offer_price(self.message, self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.review.state)
        await self.click("edit")
        await self.click("comment")
        self.message.text = "Подача включена"
        await bot.offer_comment(self.message, self.state)
        await self.click("edit")
        await self.click("phone")
        await bot.offer_phone_back(self.message, self.state)
        self.assertIn("+79990000001", self.message.answer.await_args.args[0])
        await self.click("edit")
        await self.click("phone")
        self.message.text = "89990000002"
        await bot.offer_phone(self.message, self.state)
        review = self.message.answer.await_args.args[0]
        for expected in ("📋 Проверьте ваш отклик", "Номер заявки: 1", "30000",
                         "Подача включена", "+79990000002"):
            self.assertIn(expected, review)
        stale = self.step("send", old_review)
        await bot.offer_step_action(stale, self.state, self.telegram)
        stale.answer.assert_awaited_once_with("Этот шаг уже неактуален.")
        self.assertEqual(await self.saved_offers(), [])
        self.telegram.send_message.assert_not_awaited()
        await self.click("send")
        self.assertEqual(await self.saved_offers(), [("30000", "Подача включена", "+79990000002")])

    async def test_offer_and_private_copyable_phone(self):
        await self.enter_comment()
        await self.click("skip")
        self.message.text = "8 (999) 000-00-01"
        await bot.offer_phone(self.message, self.state)
        self.assertEqual(await self.saved_offers(), [])
        self.telegram.send_message.assert_not_awaited()
        confirm_id = (await self.state.get_data())["form_keyboard_id"]
        await self.click("send")
        sends = self.telegram.send_message.await_count
        await bot.offer_step_action(self.step("send", confirm_id), self.state, self.telegram)
        self.assertEqual(self.telegram.send_message.await_count, sends)
        card = self.telegram.send_message.await_args_list[0]
        self.assertEqual(card.args[0], 10)
        self.assertNotIn("+79990000001", card.args[1])
        self.assertIn("Без комментария", card.args[1])
        contact_rows = card.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([[button.text for button in row] for row in contact_rows],
                         [["✉️ Написать исполнителю"], ["📞 Показать номер"]])
        self.assertEqual(contact_rows[0][0].url, "https://t.me/test_provider")
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(self.message.answer.await_args.kwargs["reply_markup"], bot.main_menu())
        self.assertIsNone(await storage.save_offer(1, 20, "Duplicate", "", "1", "", "+79990000002"))

        self.telegram.send_message.reset_mock()
        callback = SimpleNamespace(data=contact_rows[1][0].callback_data,
                                   from_user=SimpleNamespace(id=99), answer=AsyncMock())
        await bot.show_provider_phone(callback, self.telegram)
        callback.answer.assert_awaited_once_with("Контакт недоступен", show_alert=True)
        self.telegram.send_message.assert_not_awaited()

        callback.from_user.id = 10
        await bot.show_provider_phone(callback, self.telegram)
        sent = self.telegram.send_message.await_args
        self.assertEqual(sent.args, (10, "Телефон исполнителя: +79990000001"))
        self.assertEqual(sent.kwargs["reply_markup"].inline_keyboard[0][0].copy_text.text, "+79990000001")
