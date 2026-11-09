"""Drive real aiogram routing against a temporary SQLite DB and a fake session."""
import os
import sqlite3
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
import waste_requests as waste


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
        config = patch.object(bot, "settings", replace(
            bot.settings, token="123456:TEST", admin_ids={1, 2}, leads_channel_id=-100123,
            bot_username="OfflineTestBot", test_notification_provider_ids=frozenset(),
        ))
        config.start()
        self.addCleanup(config.stop)
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

    async def test_saved_payment_is_shown_at_moderation_and_publication(self):
        await self.send(nav.PLACE_REQUEST)
        for value in (
            "автокран", "Московская область", "Химки", "станция Химки",
            "завтра 09:00", "Работа на смену, 8 часов", payments.VAT, "+70000000000",
        ):
            await self.send(value)

        self.assertEqual(await self.sql("SELECT lead_id, method FROM lead_payment_preferences"), [(1, payments.VAT)])
        moderation_cards = [
            call.args[1] for call in self.session.await_args_list
            if call.args[1].text.startswith("Новая заявка на проверку")
        ]
        self.assertEqual({card.chat_id for card in moderation_cards}, bot.settings.admin_ids)
        for card in moderation_cards:
            self.assertIn(f"Форма оплаты: {payments.VAT}", card.text)
            self.assertIn("+70000000000", card.text)

        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=moderation_cards[0].chat_id),
            data=moderation_cards[0].reply_markup.inline_keyboard[0][0].callback_data,
            answer=AsyncMock(),
            message=SimpleNamespace(edit_text=AsyncMock()),
        )
        sent = SimpleNamespace(chat=SimpleNamespace(id=bot.settings.leads_channel_id), message_id=77)
        telegram = SimpleNamespace(send_message=AsyncMock(return_value=sent))

        await bot.publish_lead(callback, telegram)
        await bot.publish_lead(callback, telegram)

        self.assertEqual(telegram.send_message.await_count, 1)
        channel_id, public_card = telegram.send_message.await_args.args
        self.assertEqual(channel_id, bot.settings.leads_channel_id)
        self.assertIn(f"Форма оплаты: {payments.VAT}", public_card)
        self.assertNotIn("+70000000000", public_card)
        callback.message.edit_text.assert_awaited_once()
        confirmation = callback.message.edit_text.await_args.args[0]
        self.assertIn("Заявка опубликована", confirmation)
        self.assertIn(f"Форма оплаты: {payments.VAT}", confirmation)
        self.assertEqual(await self.sql("SELECT status FROM leads WHERE id = 1"), [("published",)])
        self.assertEqual(
            await self.sql("SELECT lead_id, chat_id, message_id FROM lead_publications"),
            [(1, bot.settings.leads_channel_id, 77)],
        )
        self.assertIn("уже обработана", callback.answer.await_args.args[0])

    async def test_waste_intake_persists_all_three_directions_and_moderates(self):
        for kind, material, hazard, service, mass, region, loading, destination in (
            ("soil", "Глина", None, "transport", "7.25", "Москва", waste.LOADING[1], "Площадка №1"),
            ("construction", "Асфальт", "IV", "full", waste.UNKNOWN, "Московская область",
             "Погрузка включена", waste.NO_DESTINATION),
            ("snow", "Снег с примесями", None, "container", waste.UNKNOWN, "Москва",
             waste.LOADING[0], waste.NO_DESTINATION),
        ):
            menu = await self.send(nav.PLACE_REQUEST)
            self.assertIn("самосвал", self.labels(menu))
            await self.assert_step(await self.send(waste.CATEGORY), bot.LeadForm.waste_kind)
            await self.assert_step(await self.send(waste.DIRECTIONS[kind]), bot.LeadForm.waste_material)
            if kind == "construction":
                await self.assert_step(await self.send(material), bot.LeadForm.waste_hazard)
                await self.assert_step(await self.send(hazard), bot.LeadForm.waste_service)
            else:
                await self.assert_step(await self.send(material), bot.LeadForm.waste_service)
            await self.assert_step(await self.send(nav.BACK),
                                   bot.LeadForm.waste_hazard if kind == "construction" else bot.LeadForm.waste_material)
            await self.send(hazard if kind == "construction" else material)
            await self.send(waste.SERVICES[service])
            await self.assert_step(await self.send("0"), bot.LeadForm.waste_volume)
            await self.assert_step(await self.send("12,500"), bot.LeadForm.waste_mass)
            if service == "full":
                await self.assert_step(await self.send(mass), bot.LeadForm.region)
                await self.assert_step(await self.send(nav.BACK), bot.LeadForm.waste_mass)
                await self.send(mass)
            else:
                await self.assert_step(await self.send(mass), bot.LeadForm.waste_loading)
                await self.send(loading)
            for value in (
                region, "Тестовый район", "Частный адрес погрузки", destination,
                "завтра 09:00", waste.NO_DETAILS, payments.VAT,
            ):
                await self.send(value)
            if kind == "snow":
                before = await self.sql("SELECT id FROM leads ORDER BY id")
                with patch.object(waste, "write", AsyncMock(side_effect=aiosqlite.OperationalError("simulated"))):
                    self.assertIn("Анкета открыта", (await self.send("+70000000000")).text)
                self.assertEqual(await self.sql("SELECT id FROM leads ORDER BY id"), before)
                self.assertEqual(await self.state().get_state(), bot.LeadForm.phone.state)
            await self.send("+70000000000")

            lead_id = (await self.sql("SELECT max(id) FROM leads"))[0][0]
            lead = await bot.get_lead(lead_id)
            self.assertEqual(lead["status"], "new")
            self.assertEqual(lead["region"], region)
            self.assertEqual(lead["payment_method"], payments.VAT)
            self.assertEqual(
                await self.sql(
                    """SELECT kind, material, hazard_class, service, volume_liters,
                              mass_kg, loading, destination FROM waste_lead_details WHERE lead_id = ?""",
                    (lead_id,),
                ),
                [(kind, material, hazard, service, 12500, 7250 if kind == "soil" else None,
                  loading, destination if kind == "soil" else None)],
            )
            self.assertIn("Объём: 12,5 м³", bot.lead_text(lead))
            self.assertIn(f"Форма оплаты: {payments.VAT}", bot.lead_text(lead))
            self.assertNotIn("Частный адрес погрузки", bot.lead_text(lead))
            self.assertNotIn("+70000000000", bot.lead_text(lead))
            cards = [
                call.args[1].text for call in self.session.await_args_list
                if call.args[1].text.startswith(f"Новая заявка на проверку:\n\n📌 Заявка №{lead_id}\n")
            ]
            self.assertEqual(len(cards), len(bot.settings.admin_ids))
            self.assertTrue(all("Частный адрес погрузки" in card for card in cards))
            if kind == "construction":
                self.assertTrue(all("Заявленный класс опасности: IV" in card for card in cards))
            if kind == "soil":
                callback = SimpleNamespace(
                    from_user=SimpleNamespace(id=min(bot.settings.admin_ids)),
                    data=f"publish:{lead_id}",
                    answer=AsyncMock(),
                    message=SimpleNamespace(edit_text=AsyncMock()),
                )
                sent = SimpleNamespace(chat=SimpleNamespace(id=bot.settings.leads_channel_id), message_id=88)
                telegram = SimpleNamespace(send_message=AsyncMock(return_value=sent))
                await bot.publish_lead(callback, telegram)
                await bot.publish_lead(callback, telegram)
                telegram.send_message.assert_awaited_once()
                public = telegram.send_message.await_args.args[1]
                self.assertIn(f"Форма оплаты: {payments.VAT}", public)
                self.assertNotIn("Площадка №1", public)
                self.assertNotIn("Частный адрес погрузки", public)
                self.assertNotIn("+70000000000", public)
                self.assertIn("Адрес приёма: Площадка №1", callback.message.edit_text.await_args.args[0])
        self.assertTrue(all(call.args[1].chat_id != bot.settings.leads_channel_id
                            for call in self.session.await_args_list))

    async def test_back_from_waste_to_equipment_keeps_legacy_flow(self):
        await self.send(nav.PLACE_REQUEST)
        await self.send(waste.CATEGORY)
        await self.send(waste.DIRECTIONS["snow"])
        await self.send(nav.BACK)
        await self.send(nav.BACK)
        for value in ("самосвал", "Москва", "СЗАО", "Тестовый адрес", "завтра"):
            prompt = await self.send(value)
        self.assertIn("груз, навесное оборудование", prompt.text)
        for value in ("Работы", payments.CASH, "+70000000000"):
            await self.send(value)
        self.assertEqual(await self.sql("SELECT equipment FROM leads"), [("самосвал",)])
        self.assertEqual(await self.sql("SELECT lead_id FROM waste_lead_details"), [])

    async def test_legacy_payment_is_distinct_from_any_payment(self):
        await self.add_lead()
        await self.add_lead(38)
        await payments.save_lead(38, payments.ANY, storage.DB_PATH)

        legacy = await bot.get_lead(37)
        any_payment = await bot.get_lead(38)
        self.assertIn("Форма оплаты: Не указана", bot.lead_text(legacy))
        self.assertIn(f"Форма оплаты: {payments.ANY}", bot.lead_text(any_payment))
        self.assertEqual(await self.sql("SELECT lead_id, method FROM lead_payment_preferences"), [(38, payments.ANY)])

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
        for action in (nav.PLACE_REQUEST, nav.REGISTER, "/start lead_37"):
            await self.send(action)
            self.assertIsNotNone(await self.state().get_state())
            self.assertIn(nav.PLACE_REQUEST, self.labels(await self.send(nav.HOME)))
            self.assertIsNone(await self.state().get_state())
            self.assertEqual(await self.state().get_data(), {})
        self.assertIn("Сначала зарегистрируйтесь", (await self.send(nav.ADD_MACHINE)).text)
        self.assertIsNone(await self.state().get_state())
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

    async def test_back_follows_nested_screens_and_catalog_form(self):
        await self.send(nav.HELP)
        await self.send(nav.PROVIDERS)
        await self.send(nav.PROFILE)
        self.assertIn("Раздел исполнителя", (await self.send(nav.BACK)).text)
        self.assertIn("Чтобы заказать технику", (await self.send(nav.BACK)).text)
        await self.send(nav.MY_REQUESTS)
        self.assertIn("Чтобы заказать технику", (await self.send(nav.BACK)).text)
        await self.send(nav.HOME)
        await self.send(nav.FIND_EQUIPMENT)
        await self.send(nav.PLACE_REQUEST)
        self.assertIn("каталогу готовится", (await self.send(nav.BACK)).text)

    async def test_stale_buttons_and_start_preserve_in_progress_form(self):
        await self.send(nav.PLACE_REQUEST)
        await self.send("каток")
        await self.send("Москва")
        draft = await self.state().get_data()
        for action in (nav.MY_REQUESTS, nav.REGISTER, nav.ADD_MACHINE, "/start", "/start lead_999"):
            self.assertIn("Анкета ещё открыта", (await self.send(action)).text)
            self.assertEqual(await self.state().get_state(), bot.LeadForm.area.state)
            self.assertEqual(await self.state().get_data(), draft)
        await self.send("Куркино")
        self.assertEqual((await self.state().get_data())["area"], "Куркино")
        self.assertIn("отменено", (await self.send("/cancel")).text)

    async def test_add_machine_for_blocked_provider_stays_in_menu(self):
        await self.sql(
            "INSERT INTO provider_profiles VALUES (501, 'Исполнитель', '+79990001122', 1, 1, 'old', 'old')"
        )
        self.assertIn("Профиль заблокирован", (await self.send(nav.ADD_MACHINE)).text)
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(await self.sql("SELECT id FROM provider_equipment"), [])

    async def test_lead_save_failure_is_atomic_and_keeps_form_for_retry(self):
        await self.send(nav.PLACE_REQUEST)
        for value in ("каток", "Москва", "Куркино", "ул. Тестовая", "завтра", "Укатать площадку", payments.CASH):
            await self.send(value)
        draft = await self.state().get_data()
        with patch.object(bot.lead_routing, "write", AsyncMock(side_effect=sqlite3.OperationalError("simulated"))):
            self.assertIn("Анкета открыта", (await self.send("+79991234567")).text)
        self.assertEqual(await self.state().get_data(), draft)
        self.assertEqual(await self.state().get_state(), bot.LeadForm.phone.state)
        self.assertEqual(await self.sql("SELECT * FROM leads"), [])
        self.assertEqual(await self.sql("SELECT * FROM lead_payment_preferences"), [])
        with patch.object(bot.lead_shadow_review, "evaluate", AsyncMock(side_effect=sqlite3.OperationalError("simulated"))):
            await self.send("+79991234567")
        self.assertIsNone(await self.state().get_state())
        self.assertEqual(await self.sql("SELECT status FROM leads"), [("new",)])
        self.assertEqual(await self.sql("SELECT area FROM lead_routing"), [("Куркино",)])
        self.assertEqual(await self.sql("SELECT method FROM lead_payment_preferences"), [(payments.CASH,)])
        notifications = [
            call.args[1].chat_id for call in self.session.await_args_list
            if call.args[1].text.startswith("Новая заявка на проверку")
        ]
        self.assertEqual(set(notifications), {1, 2})

    async def test_empty_allowlist_cannot_send_provider_notification(self):
        await self.add_lead()
        await self.sql("INSERT INTO lead_routing VALUES (37, 'каток', 'Москва', 'Куркино')")
        await self.sql("INSERT INTO provider_profiles VALUES (501, 'Исполнитель', '+79990001122', 1, 0, 'old', 'old')")
        await self.sql("INSERT INTO provider_equipment VALUES (8, 501, 'каток', 'ARX 23', 'Москва', 'Куркино', 1, 'old')")
        lead = await bot.get_lead(37)
        await bot.notify_test_providers(self.telegram, lead)
        self.session.assert_not_awaited()
        self.assertEqual(await self.sql("SELECT * FROM provider_notification_reservations"), [])

    async def test_sqlite_backup_preserves_existing_schema_and_all_rows(self):
        await self.add_lead()
        await self.sql("INSERT INTO providers VALUES (501, 'Прежняя анкета', 'каток', 'СЗАО', '+79990001122', 'old')")
        await self.sql("INSERT INTO provider_profiles VALUES (501, 'Исполнитель', '+79990001122', 0, 1, 'old', 'old')")
        await self.sql("INSERT INTO provider_equipment VALUES (8, 501, 'каток', 'ARX 23', 'Москва', 'Куркино', 1, 'old')")
        await self.sql("INSERT INTO provider_payment_preferences VALUES (501, ?)", (payments.VAT,))
        await self.sql("INSERT INTO lead_payment_preferences VALUES (37, ?)", (payments.CASH,))
        await self.sql("INSERT INTO lead_routing VALUES (37, 'каток', 'Москва', 'Куркино')")
        await self.sql("INSERT INTO lead_publications VALUES (37, -100123, 17)")
        await self.sql("INSERT INTO provider_notification_reservations VALUES (37, 501, 'confirmed')")
        await self.sql(
            "INSERT INTO offers VALUES (11, 37, 501, 'Исполнитель', '@offline', "
            "'25000', 'Тест', '+79990001122', 'old', 'sent')"
        )
        source = Path(storage.DB_PATH)
        copy = source.with_name("backup.sqlite")
        with sqlite3.connect(source) as original, sqlite3.connect(copy) as backup:
            original.backup(backup)
            before = list(original.iterdump())
            self.assertEqual(list(backup.iterdump()), before)
        with patch.object(storage, "DB_PATH", str(copy)):
            await bot.init_db()
            await bot.init_db()
            self.assertIn("№37", (await self.send(nav.MY_REQUESTS)).text)
            self.assertIn("ARX 23", (await self.send(nav.MY_MACHINES)).text)
            self.assertEqual(await self.sql("PRAGMA integrity_check"), [("ok",)])
        for path in (source, copy):
            with sqlite3.connect(path) as db:
                self.assertEqual(list(db.iterdump()), before)
