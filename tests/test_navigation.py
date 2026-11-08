"""Drive real aiogram routing against a temporary SQLite DB and a fake session."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram import Bot
from aiogram.types import Update

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_IDS", "1,2")
os.environ.setdefault("LEADS_CHANNEL_ID", "-100123")

import bot
import navigation as nav
import payment_preferences as payments
import provider_ui
import storage


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.dispatcher = bot.create_dispatcher()

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db_patch = patch.object(storage, "DB_PATH", str(Path(directory.name) / "bot.sqlite"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        await bot.init_db()
        self.session = AsyncMock(return_value=True)
        self.telegram = Bot("123456:TEST", session=self.session)
        self.user_id = 501
        self.update_id = 0
        await self.state().clear()

    def state(self):
        return self.dispatcher.fsm.get_context(
            bot=self.telegram, chat_id=self.user_id, user_id=self.user_id,
        )

    async def send(self, text):
        self.update_id += 1
        update = Update.model_validate({
            "update_id": self.update_id,
            "message": {
                "message_id": self.update_id,
                "date": 1700000000,
                "chat": {"id": self.user_id, "type": "private"},
                "from": {"id": self.user_id, "is_bot": False, "first_name": "Тест"},
                "text": text,
            },
        })
        count = self.session.await_count
        await self.dispatcher.feed_update(self.telegram, update)
        self.assertGreater(self.session.await_count, count, f"No reply to {text}")
        return self.session.await_args.args[1]

    async def sql(self, query, args=()):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            cursor = await db.execute(query, args)
            rows = await cursor.fetchall()
            await db.commit()
            return rows

    async def add_lead(self, lead_id=37, owner=501, status="published", equipment="каток"):
        await self.sql(
            "INSERT INTO leads VALUES (?, ?, 'Заказчик', ?, 'Москва', 'завтра', "
            "'Тестовая работа', '+79990001122', ?, '2026-11-08')",
            (lead_id, owner, equipment, status),
        )

    def labels(self, answer):
        self.assertTrue(answer.reply_markup.is_persistent)
        return [button.text for row in answer.reply_markup.keyboard for button in row]

    async def assert_step(self, answer, expected):
        self.assertEqual(await self.state().get_state(), expected.state)
        self.assertIn(nav.HOME, self.labels(answer))
        self.assertIn(nav.BACK, self.labels(answer))

    async def test_main_menu_and_sections(self):
        expected = [nav.PLACE_REQUEST, nav.FIND_EQUIPMENT, nav.PROVIDERS, nav.MY_REQUESTS, nav.ACCOUNT, nav.HELP]
        self.assertEqual(self.labels(await self.send("/start")), expected)
        for label, phrase in (
            (nav.FIND_EQUIPMENT, "каталогу готовится"),
            (nav.PROVIDERS, "Раздел исполнителя"),
            (nav.MY_REQUESTS, "пока нет заявок"),
            (nav.ACCOUNT, "Личный кабинет"),
            (nav.HELP, "Чтобы заказать технику"),
        ):
            with self.subTest(label=label):
                result = await self.send(label)
                self.assertIn(phrase, result.text)
                self.assertIn(nav.HOME, self.labels(result))
                self.assertEqual(self.labels(await self.send(nav.BACK)), expected)

    async def test_catalog_message_links_to_existing_form(self):
        answer = await self.send(nav.FIND_EQUIPMENT)
        self.assertIn(nav.PLACE_REQUEST, self.labels(answer))
        await self.assert_step(await self.send(nav.PLACE_REQUEST), bot.LeadForm.equipment)
        self.assertIn("Главное меню", (await self.send("/menu")).text)
        self.assertIsNone(await self.state().get_state())

    async def test_lead_every_step_forward_and_back(self):
        steps = list(bot.LEAD_STEPS.steps)
        inputs = ["каток", "Москва", "Куркино", "ул. Тестовая", "завтра", "Укатать площадку", payments.CASH]
        await self.assert_step(await self.send("📝 Оставить заявку"), steps[0])
        for value, step in zip(inputs, steps[1:]):
            await self.assert_step(await self.send(value), step)
        for step in reversed(steps[:-1]):
            await self.assert_step(await self.send(nav.BACK), step)
        self.assertIn("отменено", (await self.send(nav.BACK)).text)
        self.assertEqual(await self.state().get_data(), {})
        self.assertEqual(await self.sql("SELECT id FROM leads"), [])

    async def test_complete_request_returns_home_and_stays_in_moderation(self):
        await self.send(nav.PLACE_REQUEST)
        for value in ("каток", "Москва", "Куркино", "ул. Тестовая", "завтра", "Укатать площадку", payments.CASH):
            await self.send(value)
        answer = await self.send("+79991234567")
        self.assertIn(nav.PLACE_REQUEST, self.labels(answer))
        self.assertEqual(await self.sql("SELECT customer_id, status FROM leads"), [(501, "new")])
        self.assertIsNone(await self.state().get_state())
        self.assertTrue(all(call.args[1].chat_id != bot.settings.leads_channel_id
                            for call in self.session.await_args_list))

    async def test_my_requests_is_private_and_back_returns_to_account(self):
        await self.add_lead()
        await self.add_lead(38, owner=999, equipment="Чужая техника")
        await self.add_lead(39, status="new", equipment="экскаватор")
        await self.send(nav.ACCOUNT)
        answer = await self.send(nav.MY_REQUESTS)
        self.assertIn("№39 · экскаватор", answer.text)
        self.assertIn("На проверке", answer.text)
        self.assertIn("№37 · каток", answer.text)
        self.assertNotIn("Чужая техника", answer.text)
        self.assertNotIn("+79990001122", answer.text)
        self.assertEqual((await self.send(nav.BACK)).text, "Личный кабинет")

    async def test_private_account_details_are_not_sent_to_group(self):
        await self.add_lead()
        for label in (nav.MY_REQUESTS, nav.PROFILE, nav.MY_MACHINES):
            update = Update.model_validate({
                "update_id": 600,
                "message": {
                    "message_id": 600,
                    "date": 1700000000,
                    "chat": {"id": -42, "type": "group"},
                    "from": {"id": self.user_id, "is_bot": False, "first_name": "Тест"},
                    "text": label,
                },
            })
            await self.dispatcher.feed_update(self.telegram, update)
        self.session.assert_not_awaited()

    async def test_old_offer_link_navigation_and_no_duplicate(self):
        await self.add_lead()
        await self.assert_step(await self.send("/start lead_37"), bot.OfferForm.price)
        await self.assert_step(await self.send("25000"), bot.OfferForm.comment)
        await self.assert_step(await self.send("Без комментария"), bot.OfferForm.phone)
        await self.assert_step(await self.send(nav.BACK), bot.OfferForm.comment)
        await self.assert_step(await self.send(nav.BACK), bot.OfferForm.price)
        self.assertIn("отменено", (await self.send(nav.BACK)).text)
        for _ in range(2):
            await self.send("/start lead_37")
            await self.send("25000")
            await self.send("Без комментария")
            answer = await self.send("89991234567")
            self.assertIn(nav.PLACE_REQUEST, self.labels(answer))
        self.assertIn("отклик уже сохранён", answer.text)
        self.assertEqual(await self.sql("SELECT lead_id, phone FROM offers"), [(37, "+79991234567")])
        for invalid in ("/start lead_1000", "/start lead_bad", "/start obsolete"):
            self.assertIn(nav.PLACE_REQUEST, self.labels(await self.send(invalid)))

    async def test_single_provider_entry_and_registration_cancel(self):
        answer = await self.send("🚜 Я исполнитель")
        self.assertIn(nav.REGISTER, self.labels(answer))
        self.assertIsNone(await self.state().get_state())
        steps = list(provider_ui.REGISTRATION_STEPS.steps)
        await self.assert_step(await self.send(nav.REGISTER), steps[0])
        self.assertIn("согласие не получено", (await self.send("Нет")).text)
        await self.assert_step(await self.send(nav.REGISTER), steps[0])
        for value, step in zip(("Да", "Тестовый исполнитель", "+79991234567"), steps[1:]):
            await self.assert_step(await self.send(value), step)
        self.assertEqual(await self.sql("SELECT telegram_id FROM provider_profiles"), [])
        for step in reversed(steps[:-1]):
            await self.assert_step(await self.send(nav.BACK), step)
        self.assertIn(nav.REGISTER, self.labels(await self.send(nav.BACK)))
        for value in (nav.REGISTER, "Да", "Тестовый исполнитель", "+79991234567"):
            await self.send(value)
        self.assertIn(nav.REGISTER, self.labels(await self.send("/cancel")))
        self.assertEqual(await self.sql("SELECT telegram_id FROM provider_profiles"), [])

    async def test_registration_never_overwrites_existing_profile_or_methods(self):
        for value in (nav.REGISTER, "Да", "Тестовый исполнитель", "+79991234567"):
            await self.send(value)
        await self.sql(
            "INSERT INTO provider_profiles VALUES (501, 'Прежний профиль', '+79990001122', 0, 1, 'old', 'old')"
        )
        await self.sql("INSERT INTO provider_payment_preferences VALUES (501, ?)", (payments.VAT,))
        self.assertIn("уже зарегистрированы", (await self.send(payments.CASH)).text)
        self.assertIn("уже зарегистрированы", (await self.send(nav.REGISTER)).text)
        self.assertEqual(await self.sql(
            "SELECT display_name, phone, notifications_enabled, blocked, updated_at FROM provider_profiles"
        ), [("Прежний профиль", "+79990001122", 0, 1, "old")])
        self.assertEqual(await self.sql("SELECT method FROM provider_payment_preferences"), [(payments.VAT,)])

    async def test_add_equipment_backward_cancel_and_save_both_region_variants(self):
        for value in (nav.REGISTER, "Да", "Тестовый исполнитель", "+79991234567", payments.CASH):
            answer = await self.send(value)
        self.assertIn(nav.ADD_MACHINE, self.labels(answer))
        self.assertEqual(await self.sql("SELECT method FROM provider_payment_preferences"), [(payments.CASH,)])
        steps = list(provider_ui.MACHINE_STEPS.steps)
        await self.assert_step(await self.send(nav.ADD_MACHINE), steps[0])
        for value, step in zip(("каток", "ARX 23", "Москва"), steps[1:]):
            await self.assert_step(await self.send(value), step)
        for step in reversed(steps[:-1]):
            await self.assert_step(await self.send(nav.BACK), step)
        self.assertIn(nav.REGISTER, self.labels(await self.send(nav.BACK)))
        self.assertEqual(await self.sql("SELECT id FROM provider_equipment"), [])
        for region, area in (("Москва", "Куркино"), ("Москва и Московская область", None)):
            for value in (nav.ADD_MACHINE, "каток", "ARX 23", region):
                answer = await self.send(value)
            if area:
                answer = await self.send(area)
            self.assertIn("добавлена", answer.text)
            self.assertIn(nav.MY_MACHINES, self.labels(answer))
        self.assertEqual(await self.sql("SELECT area FROM provider_equipment ORDER BY id"), [
            ("Куркино",), ("весь регион",),
        ])
        self.assertIn("ARX 23", (await self.send(nav.MY_MACHINES)).text)
        self.assertIn(nav.ADD_MACHINE, self.labels(await self.send(nav.BACK)))
        self.assertIn("Наличные", (await self.send(nav.PAYMENT)).text)
        self.assertIn(nav.PAYMENT, self.labels(await self.send(nav.BACK)))
        self.assertIn("отключены", (await self.send(nav.DISABLE_NOTIFICATIONS)).text)
        self.assertIn("включены", (await self.send(nav.ENABLE_NOTIFICATIONS)).text)

    async def test_home_discards_every_form_without_writes(self):
        await self.add_lead()
        for action in (nav.PLACE_REQUEST, nav.REGISTER, nav.ADD_MACHINE, "/start lead_37"):
            await self.send(action)
            self.assertIsNotNone(await self.state().get_state())
            self.assertIn(nav.PLACE_REQUEST, self.labels(await self.send(nav.HOME)))
            self.assertIsNone(await self.state().get_state())
            self.assertEqual(await self.state().get_data(), {})
        for table in ("provider_profiles", "provider_equipment", "offers"):
            self.assertEqual(await self.sql(f"SELECT * FROM {table}"), [])
        self.assertEqual(await self.sql("SELECT id FROM leads"), [(37,)])

    async def test_existing_and_legacy_rows_are_readable_and_unchanged(self):
        await self.add_lead()
        await self.sql("INSERT INTO providers VALUES (501, 'Старая анкета', 'Старый каток', 'СЗАО', '+79990001122', 'old')")
        self.assertIn("Старая анкета", (await self.send(nav.PROFILE)).text)
        self.assertIn("Старый каток", (await self.send(nav.MY_MACHINES)).text)
        for value in (nav.REGISTER, "Да", "Текущий профиль", "+79991234567", payments.CASH,
                      nav.ADD_MACHINE, "каток", "ARX 23", "Москва", "Куркино"):
            await self.send(value)
        tables = ("leads", "providers", "provider_profiles", "provider_equipment", "provider_payment_preferences")
        before = {table: await self.sql(f"SELECT * FROM {table}") for table in tables}
        await bot.init_db()
        await bot.init_db()
        await self.send(nav.ACCOUNT)
        self.assertIn("Текущий профиль", (await self.send(nav.PROFILE)).text)
        self.assertEqual((await self.send(nav.BACK)).text, "Личный кабинет")
        answer = await self.send(nav.MY_MACHINES)
        self.assertIn("ARX 23", answer.text)
        self.assertIn("Старый каток", answer.text)
        self.assertEqual(await self.sql("SELECT equipment FROM providers"), [("Старый каток",)])
        self.assertEqual({table: await self.sql(f"SELECT * FROM {table}") for table in tables}, before)
