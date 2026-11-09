"""PR 14 regression tests with an isolated SQLite database and fake Telegram calls."""
import os
import tempfile
import unittest
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

    async def test_lead_recovers_all_steps_and_keeps_values_in_main_menu(self):
        await bot.lead_start(self.message, self.state)
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
            await self.recover_current_step()
        await self.pick_lead_option(0)
        await self.recover_current_step()
        await bot.open_navigation(self.message, self.state)
        await self.nav("home")
        self.assertIsNone(await self.state.get_state())
        keyboard = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.keyboard[0][0].text, bot.LEAD_DRAFT)
        self.message.text = bot.LEAD_DRAFT
        await bot.resume_form_from_menu(self.message, self.state)
        self.assertEqual(await self.state.get_state(), bot.LeadForm.phone.state)
        self.assertEqual((await self.state.get_data())["details"], "Работа на смену")
        self.assertEqual((await self.state.get_data())["payment_method"], bot.payment_preferences.CASH)
        self.message.text = "+79990000001"
        await bot.lead_phone(self.message, self.state, self.telegram)
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
        self.assertEqual(self.message.answer.await_args.kwargs["reply_markup"], bot.main_menu((bot.OFFER_DRAFT,)))
        self.assertEqual(await bot.draft_slot(self.state, "offer").get_state(), bot.OfferForm.comment.state)
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
