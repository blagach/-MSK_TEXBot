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
        self.message.chat = SimpleNamespace(id=20)
        self.message.bot = self.telegram
        self.message.from_user = SimpleNamespace(id=20, full_name="Исполнитель", username="test_provider")
        self.message.answer = AsyncMock(side_effect=[
            SimpleNamespace(message_id=i) for i in range(100, 120)
        ])

    def step(self, action, message_id, markup=None):
        message = MagicMock(spec=Message)
        message.message_id = message_id
        message.chat = SimpleNamespace(id=20)
        message.bot = self.telegram
        message.reply_markup = markup
        message.answer = self.message.answer
        return SimpleNamespace(message=message, data=f"offer_step:{action}", answer=AsyncMock())

    async def enter_comment(self):
        await bot.start_offer(self.message, self.state, CommandObject(args="lead_1"))
        self.assertIsInstance(self.message.answer.await_args_list[0].kwargs["reply_markup"], ReplyKeyboardRemove)
        self.message.text = "28000"
        await bot.offer_price(self.message, self.state)
        return self.message.answer.await_args.kwargs["reply_markup"]

    async def test_keyboard_replacement_back_and_home(self):
        comment_markup = await self.enter_comment()
        await bot.offer_step_action(self.step("skip", 102, comment_markup), self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.phone.state)
        self.assertEqual((await self.state.get_data())["comment"], "Без комментария")
        phone_markup = self.message.answer.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [button.callback_data for row in phone_markup.inline_keyboard for button in row],
            ["offer_step:back", "offer_step:home"],
        )
        stale = self.step("back", 102, comment_markup)
        await bot.offer_step_action(stale, self.state)
        stale.answer.assert_awaited_once_with("Этот шаг уже неактуален.")
        self.assertEqual(await self.state.get_state(), bot.OfferForm.phone.state)
        await bot.offer_step_action(self.step("back", 103, phone_markup), self.state)
        self.assertEqual(await self.state.get_state(), bot.OfferForm.comment.state)
        await bot.offer_step_action(self.step("home", 104), self.state)
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.assertEqual(self.message.answer.await_args.kwargs["reply_markup"], bot.main_menu())
        self.assertEqual(
            [call.kwargs["message_id"] for call in self.telegram.edit_message_reply_markup.await_args_list],
            [101, 102, 102, 103, 104],
        )

    async def test_offer_and_private_copyable_phone(self):
        await self.enter_comment()
        await bot.offer_step_action(self.step("skip", 102), self.state)
        self.message.text = "8 (999) 000-00-01"
        await bot.offer_phone(self.message, self.state, self.telegram)
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
