"""Help and administrator workflows with isolated SQLite and fake Telegram."""
import asyncio
import os
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, Update, CallbackQuery
from openpyxl import load_workbook

os.environ.update(BOT_TOKEN="123456:TEST", ADMIN_IDS="1,2", LEADS_CHANNEL_ID="-100123")

import admin_reports
import admin_access
import admin_ui
import bot
import payment_preferences as payments
import provider_registry
import provider_ui
import storage
import support_storage
import support_ui


class HelpAndAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Path(temp.name) / "isolated.sqlite"
        config = patch.object(storage, "DB_PATH", str(self.database))
        config.start()
        self.addCleanup(config.stop)
        await bot.init_db()
        self.telegram = Record(send_message=AsyncMock(return_value=Record(message_id=100)), edit_message_reply_markup=AsyncMock(), id=99)
        self.admins = {1, 2}
        self.memory = MemoryStorage()

    def actor(self, user_id, *, chat_type="private"):
        state = FSMContext(self.memory, StorageKey(bot_id=99, chat_id=user_id, user_id=user_id))
        message = MagicMock(spec=Message)
        message.from_user = Record(id=user_id)
        message.chat = Record(id=user_id, type=chat_type)
        message.bot = self.telegram
        message.text = None
        message.caption = None
        message.photo = None
        message.answer = AsyncMock()
        message.answer_photo = AsyncMock()
        message.answer_document = AsyncMock()
        message.edit_reply_markup = AsyncMock()
        return message, state

    def callback(self, message, data):
        return Record(message=message, from_user=message.from_user, data=data, answer=AsyncMock())

    async def help_action(self, message, state, action, group=None):
        callback = self.callback(message, f"help:{action}")
        await support_ui.support_action(callback, state, self.telegram, self.admins, group)
        return callback

    async def test_menu_access_and_preserved_role_drafts(self):
        ordinary, state = self.actor(20)
        draft_storage = bot.SQLiteDraftStorage(str(self.database))
        await draft_storage.init_schema()
        state = FSMContext(draft_storage, state.key)
        offer = bot.draft_slot(state, "offer")
        await draft_storage.checkpoint(offer.key, bot.OfferForm.price.state, {"lead_id": 2})
        await draft_storage.checkpoint(state.key, bot.LeadForm.region.state, {"equipment": "автокран"})
        await bot.open_support(ordinary, state)
        self.assertIsNone(await state.get_state())
        reopened = bot.SQLiteDraftStorage(str(self.database))
        lead = bot.draft_slot(FSMContext(reopened, state.key), "lead")
        self.assertEqual(await lead.get_state(), bot.LeadForm.region.state)
        self.assertEqual(await lead.get_data(), {"equipment": "автокран"})
        self.assertEqual(await offer.get_data(), {"lead_id": 2})
        regular_markup = ordinary.answer.await_args_list[0].kwargs["reply_markup"].keyboard
        self.assertEqual([button.text for button in regular_markup[0]], [bot.CUSTOMER_ROLE, bot.PROVIDER_ROLE])
        self.assertEqual([button.text for button in regular_markup[1]], [support_ui.HELP_LABEL])

        admin, admin_state = self.actor(1)
        await bot.open_admin_panel(admin, admin_state)
        admin_markup = admin.answer.await_args_list[0].kwargs["reply_markup"].keyboard
        self.assertIn(admin_ui.ADMIN_LABEL, [button.text for button in admin_markup[1]])
        await bot.open_admin_panel(ordinary, state)
        ordinary.answer.assert_awaited_with("Нет доступа.")

    async def test_support_badges_and_lists_follow_status_transitions(self):
        admin, admin_state = self.actor(1)
        customer, customer_state = self.actor(20)

        def buttons(message):
            keyboard = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            return {button.callback_data: button.text for row in keyboard for button in row}

        async def assert_totals(new, in_progress, closed):
            await admin_ui.admin_action(self.callback(admin, "admin:home"), self.admins)
            self.assertEqual(buttons(admin)["help:queue"], f"🛟 Поддержка · {new}")
            await self.help_action(admin, admin_state, "home")
            self.assertEqual(len(buttons(admin)), 3)
            self.assertEqual(buttons(admin)["help:resources"], "📖 Как пользоваться сервисом")
            await self.help_action(admin, admin_state, "queue")
            labels = buttons(admin)
            for status, title, count in (
                ("new", "Новое", new), ("in_progress", "В работе", in_progress), ("closed", "Закрыто", closed),
            ):
                self.assertEqual(labels[f"help:list:{status}:0"], f"{title} · {count}")

        await assert_totals(0, 0, 0)
        ticket, _ = await support_storage.create_ticket(20, "help", "new-ticket", "Тест", None)
        await assert_totals(1, 0, 0)
        await self.help_action(admin, admin_state, "list:new:0")
        self.assertTrue(any(f"help:ticket:{ticket}:" in action for action in buttons(admin)))

        await self.help_action(admin, admin_state, f"status:{ticket}:in_progress:list:new:0")
        await assert_totals(0, 1, 0)
        await self.help_action(admin, admin_state, f"status:{ticket}:closed:list:in_progress:0")
        await assert_totals(0, 0, 1)
        await self.help_action(admin, admin_state, "list:closed:0")
        self.assertTrue(any(f"help:ticket:{ticket}:" in action for action in buttons(admin)))

        await self.help_action(customer, customer_state, "home")
        self.assertNotIn("help:queue", buttons(customer))
        denied = await self.help_action(customer, customer_state, "queue")
        denied.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)

    async def test_guides_search_and_training_do_not_create_records(self):
        message, state = self.actor(20)
        for role in ("customer", "provider"):
            await self.help_action(message, state, f"guide:{role}:0")
            self.assertIn("1/6", message.answer.await_args.args[0])
            await self.help_action(message, state, f"guide:{role}:5")
            buttons = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertIn("Завершить инструкцию", [row[0].text for row in buttons])
            for step in range(4):
                await self.help_action(message, state, f"try:{role}:{step}:1")
            self.assertIn("Тренировка завершена", message.answer.await_args.args[0])
        await self.help_action(message, state, "search")
        message.text = "ОПЛАТА НДС"
        await support_ui.search_faq(message)
        keyboard = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertIn("Какие формы оплаты доступны?", [row[0].text for row in keyboard])
        async with aiosqlite.connect(self.database) as db:
            async with db.execute("SELECT (SELECT COUNT(*) FROM leads), (SELECT COUNT(*) FROM offers), (SELECT COUNT(*) FROM support_tickets)") as cursor:
                self.assertEqual(await cursor.fetchone(), (0, 0, 0))

    async def test_support_ticket_ownership_admin_reply_and_status(self):
        customer, customer_state = self.actor(20)
        await self.help_action(customer, customer_state, "new:bug")
        customer.text = "Не вижу кнопки региона"
        await support_ui.compose_ticket(customer, customer_state)
        previous = (await customer_state.get_data())["support_nonce"]
        customer.text = None
        customer.photo = [Record(file_id="screenshot")]
        await support_ui.compose_ticket(customer, customer_state)
        await self.help_action(customer, customer_state, f"send:{previous}")
        self.assertEqual(await support_storage.list_tickets(requester_id=20), [])
        nonce = (await customer_state.get_data())["support_nonce"]
        await self.help_action(customer, customer_state, f"send:{nonce}")
        await self.help_action(customer, customer_state, f"send:{nonce}")
        self.assertEqual(self.telegram.send_message.await_count, 2)
        row, = await support_storage.list_tickets(requester_id=20)
        ticket = row["id"]
        self.assertEqual((row["kind"], row["status"]), ("bug", "new"))
        self.assertEqual((await support_storage.history(ticket))[0]["photo_id"], "screenshot")

        stranger, stranger_state = self.actor(21)
        await self.help_action(stranger, stranger_state, f"ticket:{ticket}:0")
        self.assertEqual(stranger.answer.await_args.args[0], "Обращение недоступно.")
        denied = await self.help_action(stranger, stranger_state, f"reply:{ticket}")
        denied.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
        self.assertIsNone(await stranger_state.get_state())

        admin, admin_state = self.actor(1)
        await self.help_action(admin, admin_state, f"reply:{ticket}")
        admin.text = "Откройте /menu."
        admin.answer.reset_mock()
        self.telegram.send_message.reset_mock()

        async def telegram_accepts(chat_id, text, *, reply_markup):
            self.assertEqual(chat_id, customer.from_user.id)
            self.assertIn(f"Ответ поддержки по обращению №{ticket}", text)
            self.assertIn(admin.text, text)
            self.assertEqual(reply_markup.inline_keyboard[0][0].callback_data, f"help:ticket:{ticket}:0")
            self.assertEqual((await support_storage.history(ticket))[0]["delivery_status"], "sending")
            admin.answer.assert_not_awaited()
            entries = await admin_access.history(limit=50)
            self.assertFalse(any(r["action"] == "support_delivery" and r["result"] == "sent" for r in entries))
            return Record(message_id=100)

        self.telegram.send_message.side_effect = telegram_accepts
        await support_ui.reply_to_ticket(admin, admin_state, self.telegram, self.admins)
        self.telegram.send_message.assert_awaited_once()
        self.telegram.send_message.side_effect = None
        self.assertEqual(admin.answer.await_args.args[0], "Ответ сохранён и отправлен.")
        response = (await support_storage.history(ticket))[0]
        self.assertEqual(response["delivery_status"], "sent")
        delivery_audit = [r for r in await admin_access.history(limit=50) if r["action"] == "support_delivery"]
        self.assertEqual(len(delivery_audit), 1)
        self.assertEqual(delivery_audit[0]["result"], "sent")
        async with aiosqlite.connect(self.database) as db:
            async with db.execute(
                "SELECT telegram_message_id FROM support_reply_deliveries WHERE message_id=?",
                (response["id"],),
            ) as cursor:
                self.assertEqual(await cursor.fetchone(), (100,))
        await self.help_action(admin, admin_state, f"ticket:{ticket}:0")
        self.assertTrue(any(admin.text in call.args[0] for call in admin.answer.await_args_list))
        self.assertEqual((await support_storage.ticket(ticket))["status"], "in_progress")
        await self.help_action(admin, admin_state, f"status:{ticket}:closed")
        self.assertEqual((await support_storage.ticket(ticket))["status"], "closed")
        self.assertFalse(await support_storage.add_reply(ticket, 1, "late", "Поздний ответ"))
        await self.help_action(customer, customer_state, f"ticket:{ticket}:0")
        self.assertTrue(any("Откройте /menu." in call.args[0] for call in customer.answer.await_args_list))
        customer.answer_photo.assert_awaited_once_with("screenshot")

    async def test_support_notification_opens_requester_history_through_dispatcher(self):
        ticket, _ = await support_storage.create_ticket(20, "bug", "dispatcher", "Пропал ответ", None)
        stamp = int(datetime(2026, 11, 10, tzinfo=timezone.utc).timestamp())
        delivered = []

        def user(user_id):
            return {"id": user_id, "is_bot": False, "first_name": "Тест"}

        def chat(user_id):
            return {"id": user_id, "type": "private"}

        def callback(update_id, user_id, data):
            return Update.model_validate({"update_id": update_id, "callback_query": {
                "id": str(update_id), "from": user(user_id), "chat_instance": str(user_id), "data": data,
                "message": {"message_id": update_id, "date": stamp, "chat": chat(user_id)},
            }})

        async def transport(_bot, method, **_kwargs):
            if isinstance(method, SendMessage):
                delivered.append(method)
                return Message(message_id=500 + len(delivered), date=stamp, chat=chat(method.chat_id))
            self.assertEqual(method.__api_method__, "answerCallbackQuery")
            return True

        fsm = bot.SQLiteDraftStorage(str(self.database))
        await fsm.init_schema()
        response = "Сохранённый ответ из уведомления"
        async with Bot("99:TEST") as telegram:
            with ExitStack() as stack:
                for router in (bot.router, provider_ui.router, support_ui.router, admin_ui.router):
                    stack.enter_context(patch.object(router, "_parent_router", None))
                stack.enter_context(patch.object(telegram.session, "make_request", side_effect=transport))
                dispatcher = bot.create_dispatcher(fsm)
                await dispatcher.feed_update(telegram, callback(1, 1, f"help:reply:{ticket}"))
                await dispatcher.feed_update(telegram, Update.model_validate({"update_id": 2, "message": {
                    "message_id": 2, "date": stamp, "chat": chat(1), "from": user(1), "text": response,
                }}))
                notifications = [method for method in delivered if method.chat_id == 20]
                self.assertEqual(len(notifications), 1)
                self.assertIn(response, notifications[0].text)
                link = notifications[0].reply_markup.inline_keyboard[0][0].callback_data
                await dispatcher.feed_update(telegram, callback(3, 20, link))
                self.assertTrue(any(method.chat_id == 20 and method.text == f"№{ticket} · Поддержка\n{response}"
                                    for method in delivered))
        self.assertEqual((await support_storage.history(ticket))[0]["delivery_status"], "sent")

    async def test_tz7_support_menu_keeps_old_features_in_guide(self):
        user, state = self.actor(20)
        for user_id in (20, 1):
            await support_ui.menu(user, user_id, self.admins)
            rows = user.answer.await_args.kwargs["reply_markup"].inline_keyboard
            self.assertEqual([button.text for row in rows for button in row], [
                "📖 Как пользоваться сервисом", "📋 Мои обращения", "💬 Написать в поддержку",
            ])
        await self.help_action(user, state, "resources")
        rows = user.answer.await_args.kwargs["reply_markup"].inline_keyboard
        actions = {button.callback_data for row in rows for button in row}
        self.assertTrue({"help:guides", "help:faq", "help:training", "help:new:bug"} <= actions)

    async def test_tz7_requester_closes_reopens_without_losing_history(self):
        ticket, _ = await support_storage.create_ticket(20, "help", "tz7-owner", "Вопрос", "photo")
        await support_storage.add_reply(ticket, 1, "tz7-answer", "Ответ")
        before = [(row["id"], row["text"], row["photo_id"]) for row in await support_storage.history(ticket)]
        stranger, foreign_state = self.actor(21)
        version = (await support_storage.ticket(ticket))["status_version"]
        await self.help_action(stranger, foreign_state, f"owner_confirm:{ticket}:{version}")
        self.assertEqual((await support_storage.ticket(ticket))["status"], "in_progress")

        owner, state = self.actor(20)
        await self.help_action(owner, state, f"owner_close:{ticket}:{version}")
        self.assertEqual((await support_storage.ticket(ticket))["status"], "in_progress")
        await self.help_action(owner, state, f"owner_confirm:{ticket}:{version}")
        self.assertEqual((await support_storage.ticket(ticket))["status"], "closed")
        self.telegram.send_message.reset_mock()
        closed_version = (await support_storage.ticket(ticket))["status_version"]
        await self.help_action(owner, state, f"owner_reopen:{ticket}:{closed_version}")
        await self.help_action(owner, state, f"owner_reopen:{ticket}:{closed_version}")
        await self.help_action(owner, state, f"owner_confirm:{ticket}:{version}")
        self.assertEqual((await support_storage.ticket(ticket))["status"], "new")
        self.assertEqual(self.telegram.send_message.await_count, len(self.admins))
        self.assertEqual(before, [(row["id"], row["text"], row["photo_id"])
                                  for row in await support_storage.history(ticket)])
        async with aiosqlite.connect(self.database) as db:
            events = await (await db.execute(
                "SELECT actor_id,status FROM support_status_events WHERE ticket_id=? ORDER BY id", (ticket,),
            )).fetchall()
        self.assertEqual(events, [(1, "in_progress"), (20, "closed"), (20, "new")])

    async def test_requester_status_race_records_only_one_event(self):
        ticket, _ = await support_storage.create_ticket(20, "help", "tz7-race", "Вопрос", None)
        version = (await support_storage.ticket(ticket))["status_version"]
        results = await asyncio.gather(*(
            support_storage.requester_status(ticket, 20, "closed", version) for _ in range(2)
        ))
        self.assertEqual(sorted(results), [False, True])
        current = await support_storage.ticket(ticket)
        self.assertFalse(await support_storage.requester_status(ticket, 21, "new", current["status_version"]))
        self.assertTrue(await support_storage.requester_status(ticket, 20, "new", current["status_version"]))
        self.assertFalse(await support_storage.requester_status(ticket, 20, "closed", version))

    async def test_delivery_error_and_legacy_history_are_persisted(self):
        ticket_id, _ = await support_storage.create_ticket(20, "bug", "first", "Question", None)
        async with aiosqlite.connect(self.database) as db:
            await db.execute(
                "INSERT INTO support_messages(ticket_id,request_id,actor_id,is_admin,text,created_at) "
                "VALUES (?,'old',1,1,'Historical reply','2026-11-09')", (ticket_id,),
            )
            await db.commit()
        await support_storage.init_schema()
        self.assertEqual(await support_storage.queued_deliveries(), [])
        self.assertIsNone((await support_storage.history(ticket_id))[0]["delivery_status"])
        request = SendMessage(chat_id=20, text="x")
        for error, expected, code, notice in (
            (TelegramForbiddenError(request, "Forbidden: bot was blocked by the user"), "failed", "bot_blocked",
             "Уведомление не отправлено."),
            (TelegramNetworkError(request, "timeout"), "uncertain", "TelegramNetworkError",
             "Отправка уведомления не подтверждена."),
            (TimeoutError(), "uncertain", "TimeoutError", "Отправка уведомления не подтверждена."),
            (support_ui.IsolationError("blocked by test guard"), "failed", "IsolationError",
             "Уведомление не отправлено."),
        ):
            self.telegram.send_message.side_effect = error
            admin, state = self.actor(1)
            await self.help_action(admin, state, f"reply:{ticket_id}")
            admin.text = f"Ответ для проверки {code}"
            await support_ui.reply_to_ticket(admin, state, self.telegram, self.admins)
            self.assertEqual(admin.answer.await_args.args[0], f"Ответ сохранён в истории. {notice}")
            self.assertEqual(self.telegram.send_message.await_args.args[0], 20)
            row = (await support_storage.history(ticket_id))[0]
            self.assertEqual((row["delivery_status"], row["delivery_error"]), (expected, code))
            audit = [r for r in await admin_access.history(limit=50) if r["action"] == "support_delivery"][0]
            self.assertEqual(audit["result"], expected)
            self.assertIn(code, audit["details"])

            await self.help_action(admin, state, f"ticket:{ticket_id}:0")
            admin_history = [call.args[0] for call in admin.answer.await_args_list]
            self.assertTrue(any(admin.text in item and code in item for item in admin_history))
            customer, customer_state = self.actor(20)
            await self.help_action(customer, customer_state, f"ticket:{ticket_id}:0")
            self.assertIn(
                f"№{ticket_id} · Поддержка\n{admin.text}",
                [call.args[0] for call in customer.answer.await_args_list],
            )
            self.assertIsNone(await support_ui.deliver_reply(self.telegram, row["id"]))
        self.assertEqual(self.telegram.send_message.await_count, 4)

    async def test_equipment_catalog_retires_old_panels_and_ignores_their_callbacks(self):
        provider, state = self.actor(20)
        provider.answer.side_effect = [Record(message_id=i) for i in range(200, 205)]
        await provider_ui.machine_catalog(provider, state)

        provider.message_id = 200
        await provider_ui.machine_catalog_action(self.callback(provider, "machine_catalog:0"), state)
        self.telegram.edit_message_reply_markup.assert_awaited_once_with(
            chat_id=20, message_id=200, reply_markup=None,
        )
        stale = self.callback(provider, "machine_catalog:1")
        await provider_ui.machine_catalog_action(stale, state)
        stale.answer.assert_awaited_once_with("Этот шаг уже неактуален.")
        self.assertEqual((await state.get_data())["machine_panel"], 201)

        provider.message_id = 201
        await provider_ui.machine_catalog_action(self.callback(provider, "machine_catalog:back"), state)
        provider.message_id = 202
        await provider_ui.machine_catalog_action(self.callback(provider, "machine_catalog:0"), state)
        provider.message_id = 203
        await provider_ui.machine_catalog_action(self.callback(provider, "machine_catalog:0"), state)

        self.assertEqual(await state.get_state(), provider_ui.AddMachine.description.state)
        self.assertEqual((await state.get_data())["category"], "мини-экскаватор")
        self.assertIsNone((await state.get_data())["machine_panel"])
        self.assertEqual([call.kwargs["message_id"] for call in
                          self.telegram.edit_message_reply_markup.await_args_list], [200, 201, 202, 203])

    async def test_provider_payment_buttons_keep_draft_until_saved(self):
        provider, state = self.actor(20)
        await provider_registry.register_provider(20, "Тест", "+79990000000", True, db_path=self.database)
        await payments.save_provider(20, {payments.CASH}, self.database)
        provider.message_id = 10
        provider.answer.side_effect = [Record(message_id=i) for i in range(11, 18)]

        async def current_methods():
            async with aiosqlite.connect(self.database) as db:
                async with db.execute("SELECT method FROM provider_payment_preferences WHERE provider_id=20") as cursor:
                    return {row[0] for row in await cursor.fetchall()}

        async def click(action, panel):
            provider.message_id = panel
            await provider_ui.payment_selection_callback(self.callback(provider, f"provider_payment:{action}"), state)

        await provider_ui.edit_payment_methods(self.callback(provider, "provider_payment:edit"), state)
        await click("pick:1", 11)
        provider.edit_reply_markup.assert_awaited_once()
        self.assertEqual(provider.answer.await_count, 1)
        await click("pick:2", 10)  # An older screen must not change the selection.
        self.assertEqual(set((await state.get_data())["payment_selection"]), {payments.CASH, payments.VAT})
        self.assertEqual(await current_methods(), {payments.CASH})
        await click("cancel", 11)
        self.telegram.edit_message_reply_markup.assert_awaited_with(chat_id=20, message_id=11, reply_markup=None)
        self.assertEqual(await current_methods(), {payments.CASH})

        await provider_ui.edit_payment_methods(self.callback(provider, "provider_payment:edit"), state)
        provider.edit_reply_markup.side_effect = TelegramNetworkError(SendMessage(chat_id=20, text="x"), "timeout")
        await click("pick:2", 13)
        self.assertEqual((await state.get_data())["payment_panel"], 14)
        await click("save", 13)
        self.assertEqual(await current_methods(), {payments.CASH})
        await click("save", 14)
        self.telegram.edit_message_reply_markup.assert_awaited_with(chat_id=20, message_id=14, reply_markup=None)
        self.assertEqual(await current_methods(), {payments.CASH, payments.NO_VAT})

        await provider_ui.edit_payment_methods(self.callback(provider, "provider_payment:edit"), state)
        await bot.pause_form(provider, state)
        self.telegram.edit_message_reply_markup.assert_awaited_with(chat_id=20, message_id=16, reply_markup=None)
        self.assertIsNone(await state.get_state())
        self.assertEqual(set((await bot.draft_slot(state, "provider_action").get_data())["payment_selection"]),
                         {payments.CASH, payments.NO_VAT})

    async def test_restart_requeues_only_unsent_and_claim_is_unique(self):
        ticket_id, _ = await support_storage.create_ticket(20, "help", "first", "Question", None)
        interrupted = await support_storage.add_reply(ticket_id, 1, "r1", "First answer")
        queued = await support_storage.add_reply(ticket_id, 1, "r2", "Second answer")
        self.assertFalse(await support_storage.add_reply(ticket_id, 1, "r2", "Duplicate"))
        self.assertIsNotNone(await support_storage.claim_delivery(interrupted))
        await support_storage.recover_deliveries()
        self.assertEqual(await support_storage.queued_deliveries(), [queued])
        outcomes = await asyncio.gather(
            support_ui.deliver_reply(self.telegram, queued), support_ui.deliver_reply(self.telegram, queued),
        )
        self.assertCountEqual(outcomes, ["sent", None])
        self.telegram.send_message.assert_awaited_once()
        rows = await support_storage.history(ticket_id)
        self.assertEqual((rows[1]["delivery_status"], rows[1]["delivery_error"]), ("uncertain", "interrupted"))

    async def test_edit_invalidates_old_send_and_ticket_returns_to_filtered_page(self):
        user, state = self.actor(20)
        await self.help_action(user, state, "new:help")
        user.text = "Before"
        await support_ui.compose_ticket(user, state)
        old_nonce = (await state.get_data())["support_nonce"]
        await self.help_action(user, state, f"edit:{old_nonce}")
        await self.help_action(user, state, f"send:{old_nonce}")
        self.assertEqual(await support_storage.list_tickets(requester_id=20), [])
        user.text = "After"
        await support_ui.compose_ticket(user, state)
        await self.help_action(user, state, f"send:{(await state.get_data())['support_nonce']}")
        ticket_id = (await support_storage.list_tickets(requester_id=20))[0]["id"]
        self.assertEqual((await support_storage.history(ticket_id))[0]["text"], "After")
        admin, state = self.actor(1)
        await self.help_action(admin, state, f"ticket:{ticket_id}:0:list:new:8")
        markup = admin.answer.await_args.kwargs["reply_markup"]
        self.assertIn("help:list:new:8", [b.callback_data for row in markup.inline_keyboard for b in row])
        self.assertIn("help:queue", [b.callback_data for row in markup.inline_keyboard for b in row])
        await self.help_action(admin, state, f"status:{ticket_id}:in_progress:list:new:8")
        markup = admin.answer.await_args.kwargs["reply_markup"]
        self.assertIn("help:list:new:8", [b.callback_data for row in markup.inline_keyboard for b in row])

    async def test_simultaneous_confirmations_are_idempotent(self):
        results = await asyncio.gather(*[
            support_storage.create_ticket(20, "help", "same", "Текст", None) for _ in range(2)
        ])
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(created for _, created in results), 1)

    async def test_owner_confirms_roles_and_revoked_admin_cannot_export_or_moderate(self):
        owner, state = self.actor(1)
        owner.answer.return_value = Record(message_id=500)
        await admin_ui.admin_action(self.callback(owner, "admin:role:support"), self.admins,
                                    state, service_owner_id=1)
        owner.text = "42"
        await admin_ui.admin_target(owner, state, self.admins, service_owner_id=1)
        self.assertFalse(await admin_access.allowed(42, "support", self.admins, 1))
        nonce = (await state.get_data())["admin_nonce"]
        owner.message_id = 500
        confirm = self.callback(owner, f"admin:confirm:{nonce}")
        await admin_ui.admin_action(confirm, self.admins, state, service_owner_id=1)
        self.assertTrue(await admin_access.allowed(42, "support", self.admins, 1))
        await admin_ui.admin_action(confirm, self.admins, state, service_owner_id=1)
        role_events = [row for row in await admin_access.history(2**63 - 1, 100) if row["action"] == "role_changed"]
        self.assertEqual(len(role_events), 1)

        admin, admin_state = self.actor(2)
        await admin_access.assign_role(1, 2, "support", "restrict-legacy", owner_id=1)
        denied = self.callback(admin, "admin:export")
        await admin_ui.admin_action(denied, self.admins, admin_state, service_owner_id=1)
        denied.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
        admin.answer_document.assert_not_awaited()
        publish = self.callback(admin, "publish:1")
        await bot.publish_lead(publish, self.telegram)
        publish.answer.assert_awaited_once_with("Нет доступа", show_alert=True)
        self.telegram.send_message.assert_not_awaited()

    async def test_admin_confirmation_survives_menu_and_old_controls_cannot_cancel_it(self):
        owner, state = self.actor(1)
        owner.answer.return_value = Record(message_id=500)
        await admin_ui.admin_action(self.callback(owner, "admin:role:moderator"), self.admins,
                                    state, service_owner_id=1)
        owner.text = "42"
        await admin_ui.admin_target(owner, state, self.admins, service_owner_id=1)
        old_nonce = (await state.get_data())["admin_nonce"]
        await admin_ui.admin_action(self.callback(owner, "admin:home"), self.admins,
                                    state, service_owner_id=1)
        draft = bot.draft_slot(state, "admin_action")
        self.assertEqual(await draft.get_state(), admin_ui.ManageAdmins.confirm.state)
        with patch.object(bot, "admin_allowed", new_callable=AsyncMock, return_value=True):
            owner.text = bot.ADMIN_ACTION_DRAFT
            await bot.resume_auxiliary_action(owner, state)
        new_nonce = (await state.get_data())["admin_nonce"]
        self.assertNotEqual(new_nonce, old_nonce)
        owner.message_id = 500
        await admin_ui.admin_action(self.callback(owner, f"admin:role_cancel:{old_nonce}"),
                                    self.admins, state, service_owner_id=1)
        self.assertEqual(await state.get_state(), admin_ui.ManageAdmins.confirm.state)
        await admin_ui.admin_action(self.callback(owner, f"admin:confirm:{old_nonce}"),
                                    self.admins, state, service_owner_id=1)
        self.assertFalse(await admin_access.allowed(42, "moderate", self.admins, 1))
        await admin_ui.admin_action(self.callback(owner, f"admin:role_cancel:{new_nonce}"),
                                    self.admins, state, service_owner_id=1)
        self.assertIsNone(await state.get_state())
        self.assertFalse(await admin_access.allowed(42, "moderate", self.admins, 1))

    async def test_old_role_confirmation_cannot_override_later_revocation(self):
        owner, state = self.actor(1)
        owner.answer.return_value = Record(message_id=500)
        await admin_ui.admin_action(self.callback(owner, "admin:role:administrator"), self.admins,
                                    state, service_owner_id=1)
        owner.text = "2"
        await admin_ui.admin_target(owner, state, self.admins, service_owner_id=1)
        nonce = (await state.get_data())["admin_nonce"]
        self.assertIn("Текущая роль: Администратор", owner.answer.await_args.args[0])
        await admin_access.assign_role(1, 2, "none", "later-revocation", owner_id=1)
        owner.message_id = 500
        await admin_ui.admin_action(self.callback(owner, f"admin:confirm:{nonce}"), self.admins,
                                    state, service_owner_id=1)
        self.assertFalse(await admin_access.allowed(2, "export", self.admins, 1))
        self.assertEqual(await state.get_state(), admin_ui.ManageAdmins.target.state)
        self.assertIn("Права уже изменились", owner.answer.await_args.args[0])

    async def test_legacy_role_draft_requires_new_review_without_losing_target(self):
        owner, state = self.actor(1)
        await state.set_state(admin_ui.ManageAdmins.confirm)
        await state.set_data({"admin_target": 42, "admin_role": "support", "admin_nonce": "legacy"})
        await admin_ui.resume_admin_step(owner, state)
        self.assertEqual(await state.get_state(), admin_ui.ManageAdmins.target.state)
        self.assertEqual((await state.get_data())["admin_target"], 42)
        self.assertFalse(await admin_access.allowed(42, "support", self.admins, 1))

    async def test_admin_counters_are_personal_and_support_role_sees_only_support(self):
        async with aiosqlite.connect(self.database) as db:
            await db.executemany(
                """INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,status,created_at)
                   VALUES (20,'автокран','Москва','завтра','','+79990000001',?,'2026-11-11')""",
                [("new",), ("published",), ("closed",)],
            )
            await db.commit()
        owner, _ = self.actor(1)
        other, _ = self.actor(2)
        await admin_ui.menu(owner, 1, self.admins, 1)
        labels = [button.text for row in owner.answer.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("📋 Заявки · 2 / 2", labels)
        self.assertIn("📋 Заявки · 2 / 2", labels)
        await admin_access.mark_lead_view(1, 1)
        await admin_ui.menu(owner, 1, self.admins, 1)
        labels = [button.text for row in owner.answer.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("📋 Заявки · 2 / 1", labels)
        await admin_ui.menu(other, 2, self.admins, 1)
        labels = [button.text for row in other.answer.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertIn("📋 Заявки · 2 / 2", labels)
        await admin_access.assign_role(1, 2, "support", "support-only", owner_id=1)
        await admin_ui.menu(other, 2, self.admins, 1)
        buttons = [button.callback_data for row in other.answer.await_args.kwargs["reply_markup"].inline_keyboard for button in row]
        self.assertEqual(buttons, ["help:queue"])

    async def test_role_menu_updates_after_grant_and_revocation_outside_admin_panel(self):
        await admin_access.assign_role(1, 42, "support", "grant-new-staff", owner_id=1)
        staff, state = self.actor(42)
        await bot.open_support(staff, state)
        rows = staff.answer.await_args_list[0].kwargs["reply_markup"].keyboard
        self.assertIn(admin_ui.ADMIN_LABEL, [button.text for row in rows for button in row])
        await admin_access.assign_role(1, 42, "none", "revoke-new-staff", owner_id=1)
        staff.answer.reset_mock()
        await bot.open_support(staff, state)
        rows = staff.answer.await_args_list[0].kwargs["reply_markup"].keyboard
        self.assertNotIn(admin_ui.ADMIN_LABEL, [button.text for row in rows for button in row])

    async def test_audit_pages_keep_older_events_and_require_owner(self):
        for n in range(14):
            await admin_access.record(1, f"event_{n}")
        owner, state = self.actor(1)
        await admin_ui.admin_action(self.callback(owner, "admin:audit"), self.admins,
                                    state, service_owner_id=1)
        markup = owner.answer.await_args.kwargs["reply_markup"]
        earlier = markup.inline_keyboard[0][0].callback_data
        self.assertTrue(earlier.startswith("admin:audit:"))
        await admin_ui.admin_action(self.callback(owner, earlier), self.admins,
                                    state, service_owner_id=1)
        self.assertIn("event_0", owner.answer.await_args.args[0])
        ordinary, other_state = self.actor(2)
        denied = self.callback(ordinary, earlier)
        await admin_ui.admin_action(denied, self.admins, other_state, service_owner_id=1)
        denied.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
        ordinary.answer.assert_not_awaited()

    async def test_owner_can_inspect_roles_and_audit_but_other_staff_cannot(self):
        await admin_access.assign_role(1, 2, "support", "assign-support", owner_id=1, legacy_admins=self.admins)
        owner, state = self.actor(1)
        await admin_ui.admin_action(self.callback(owner, "admin:admins:0"), self.admins,
                                    state, service_owner_id=1)
        self.assertIn("ID 1 · Владелец", owner.answer.await_args.args[0])
        self.assertIn("ID 2 · Поддержка", owner.answer.await_args.args[0])
        await admin_ui.admin_action(self.callback(owner, "admin:audit"), self.admins,
                                    state, service_owner_id=1)
        self.assertIn("Администратор → Поддержка", owner.answer.await_args.args[0])
        self.assertIn("Изменение прав", owner.answer.await_args.args[0])
        staff, staff_state = self.actor(2)
        denied = self.callback(staff, "admin:admins:0")
        await admin_ui.admin_action(denied, self.admins, staff_state, service_owner_id=1)
        denied.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
        staff.answer.assert_not_awaited()

    async def test_support_role_revoked_during_reply_cannot_send_and_keeps_draft(self):
        ticket, _ = await support_storage.create_ticket(20, "help", "role-check", "Вопрос", None)
        staff, state = self.actor(2)
        await state.set_state(support_ui.Support.reply)
        await state.set_data({"support_ticket": ticket, "support_nonce": "reply-check"})
        staff.text = "Ответ"
        before = await support_storage.history(ticket)
        await admin_access.assign_role(1, 2, "none", "revoke-during-reply", owner_id=1)
        await support_ui.reply_to_ticket(staff, state, self.telegram, self.admins)
        staff.answer.assert_awaited_once_with("Нет доступа.")
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(await state.get_state(), support_ui.Support.reply.state)
        self.assertEqual(len(await support_storage.history(ticket)), len(before))

    async def test_user_search_uses_shared_excel_contact_rules_and_literal_unicode_search(self):
        async with aiosqlite.connect(self.database) as db:
            await db.execute("INSERT INTO provider_profiles(telegram_id,display_name,phone,consent_at,updated_at) "
                             "VALUES (42,'ООО Ёлка','8 (999) 123-45-67','2026-11-11','2026-11-11')")
            await db.execute("INSERT INTO leads(customer_id,customer_name,equipment,place,work_date,details,phone,status,created_at) "
                             "VALUES (42,'Старое имя','автокран','Москва','завтра','','+79991234567','published','2026-11-11')")
            await db.execute("INSERT INTO lead_work_details(lead_id,customer_username) VALUES (1,'test_company')")
            await db.commit()
        for query in ("елка", "42", "+79991234567", "8 (999) 123-45-67", "@TEST_COMPANY"):
            self.assertEqual([r["id"] for r in await admin_reports.user_page(query)], [42])
        self.assertEqual(await admin_reports.user_page("%' OR 1=1 --"), [])
        self.assertEqual(await admin_reports.user_page("%"), [])
        profile = await admin_reports.user_profile(42)
        self.assertEqual((profile["leads"], profile["offers"], profile["customer"], profile["performer"]), (1,0,1,1))
        self.assertEqual(profile["phone"], "+79991234567")
        self.assertIsNone(await admin_reports.user_profile(43))

    async def test_user_search_survives_sqlite_restart_and_retired_pages_do_not_apply(self):
        from draft_storage import SQLiteDraftStorage
        backing = SQLiteDraftStorage(str(self.database))
        await backing.init_schema()
        owner, _ = self.actor(1)
        owner.answer.return_value = Record(message_id=500)
        state = FSMContext(backing, StorageKey(bot_id=99, chat_id=1, user_id=1))
        await state.set_state(admin_ui.FindUser.query)
        owner.text = "Нет такого имени"
        await admin_ui.find_user(owner, state, self.admins)
        old = (await state.get_data())["user_nonce"]
        await bot.pause_form(owner, state)
        restarted = SQLiteDraftStorage(str(self.database))
        await restarted.init_schema()
        restored = FSMContext(restarted, state.key)
        owner.text = bot.ADMIN_SEARCH_DRAFT
        await bot.resume_auxiliary_action(owner, restored)
        data = await restored.get_data()
        self.assertEqual(data["user_query"], "Нет такого имени")
        self.assertNotEqual(data["user_nonce"], old)
        owner.message_id = 500
        await admin_ui.admin_action(self.callback(owner, f"admin:people:42:{old}"), self.admins, restored)
        self.assertIn("поиск устарел", owner.answer.await_args.args[0])

    async def test_user_profile_and_search_deny_support_only_role_and_keep_existing_form(self):
        await admin_access.assign_role(1, 2, "support", "support-only-search", owner_id=1)
        staff, state = self.actor(2)
        for action in ("admin:find", "admin:user:42", "admin:users_filter:all"):
            denied = self.callback(staff, action)
            await admin_ui.admin_action(denied, self.admins, state)
            denied.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
        staff.answer.assert_not_awaited()
        owner, other_state = self.actor(1)
        await other_state.set_state(bot.OfferForm.price)
        await other_state.set_data({"lead_id": 11})
        await admin_ui.admin_action(self.callback(owner,"admin:find"), self.admins, other_state)
        self.assertEqual((await other_state.get_data())["lead_id"],11)
        self.assertEqual(await other_state.get_state(),bot.OfferForm.price.state)

    async def test_access_change_requires_reason_confirmation_and_current_version(self):
        await provider_registry.register_provider(42,"Тест","+79991234567",True,str(self.database))
        owner,state=self.actor(1)
        owner.answer.return_value=Record(message_id=500)
        owner.message_id=500
        async def action(prefix):
            nonce=(await state.get_data()).get("access_nonce")
            await admin_ui.admin_action(self.callback(owner,f"admin:{prefix}:{nonce}"),self.admins,state,service_owner_id=1)
        await admin_ui.admin_action(self.callback(owner,"admin:access:42"),self.admins,state,service_owner_id=1)
        await action("access_pick:requests")
        self.assertTrue(await bot.user_access.allowed(42,"requests"))
        await action("access_next")
        owner.text="Тестовое ограничение"
        await admin_ui.access_reason(owner,state,self.admins,service_owner_id=1)
        self.assertTrue(await bot.user_access.allowed(42,"requests"))
        await action("access_confirm")
        self.assertFalse(await bot.user_access.allowed(42,"requests"))
        self.assertTrue(await bot.user_access.allowed(42,"offers"))
        self.assertEqual([r["id"] for r in await admin_reports.user_page(kind="restricted")],[42])
        await admin_ui.admin_action(self.callback(owner,"admin:access:42"),self.admins,state,service_owner_id=1)
        await action("access_pick:clear")
        await action("access_next")
        owner.text="Снять ограничение"
        await admin_ui.access_reason(owner,state,self.admins,service_owner_id=1)
        revision=(await bot.user_access.snapshot(42))["revision"]
        await bot.user_access.change(1,42,["full"],"Более позднее решение","later",expected_revision=revision,owner_id=1)
        await action("access_confirm")
        self.assertEqual(await state.get_state(),admin_ui.ManageAccess.choose.state)
        self.assertEqual((await bot.user_access.snapshot(42))["scopes"],["full"])
        await action("access_cancel")
        self.assertEqual((await bot.user_access.snapshot(42))["scopes"],["full"])

    async def test_access_management_is_owner_only_and_survives_menu(self):
        await provider_registry.register_provider(42,"Тест","+79991234567",True,str(self.database))
        staff,staff_state=self.actor(2)
        denied=self.callback(staff,"admin:access:42")
        await admin_ui.admin_action(denied,self.admins,staff_state,service_owner_id=1)
        denied.answer.assert_awaited_once_with("Нет доступа.",show_alert=True)
        owner,state=self.actor(1)
        owner.answer.return_value=Record(message_id=500)
        await admin_ui.admin_action(self.callback(owner,"admin:access:42"),self.admins,state,service_owner_id=1)
        old=(await state.get_data())["access_nonce"]
        await bot.pause_form(owner,state)
        owner.text=bot.ADMIN_ACCESS_DRAFT
        with patch.object(bot,"admin_allowed",new_callable=AsyncMock,return_value=True):
            await bot.resume_auxiliary_action(owner,state)
        self.assertEqual((await state.get_data())["access_target"],42)
        self.assertNotEqual((await state.get_data())["access_nonce"],old)
        owner.message_id=500
        await admin_ui.admin_action(self.callback(owner,f"admin:access_confirm:{old}"),self.admins,state,service_owner_id=1)
        self.assertEqual((await bot.user_access.snapshot(42))["revision"],0)

    async def test_restriction_gates_preserve_forms_and_allow_support(self):
        customer,state=self.actor(42)
        await state.set_state(bot.LeadForm.region)
        await state.set_data({"equipment":"автокран"})
        await bot.user_access.change(1,42,["full"],"Тест","full",expected_revision=0,owner_id=1)
        customer.text="Москва"
        handler=AsyncMock()
        await bot.check_user_access(handler,customer,{"state":state})
        handler.assert_not_awaited()
        self.assertEqual((await state.get_data())["equipment"],"автокран")
        await state.set_state(support_ui.Support.compose)
        old_button=MagicMock(spec=CallbackQuery)
        old_button.message=customer
        old_button.from_user=customer.from_user
        old_button.data="provider:old_action"
        old_button.answer=AsyncMock()
        await bot.check_user_access(handler,old_button,{"state":state})
        old_button.answer.assert_awaited_once()
        handler.assert_not_awaited()
        customer.text="/support"
        await bot.check_user_access(handler,customer,{"state":state})
        handler.assert_awaited_once()
        await bot.submit_lead(customer,customer.from_user,state,self.telegram,{})
        await bot.send_offer(customer,customer.from_user,state,self.telegram)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual((await state.get_data())["equipment"],"автокран")

    async def test_owner_panel_has_eight_sections_in_agreed_order_and_safe_finance_marketing(self):
        owner,state=self.actor(1)
        await admin_ui.menu(owner,1,self.admins,1)
        rows=owner.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([len(row) for row in rows],[2,2,2,2])
        self.assertEqual([[b.callback_data for b in row] for row in rows],[
            ["admin:stats","admin:records"],["admin:users","help:queue"],
            ["admin:equipment:0","admin:finance"],["admin:marketing","admin:settings"]])
        for action,expected in (("admin:finance","Реальные платежи не подключены"),("admin:marketing","Отправка отключена")):
            await admin_ui.admin_action(self.callback(owner,action),self.admins,state,service_owner_id=1)
            self.assertIn(expected,owner.answer.await_args.args[0])
        self.telegram.send_message.assert_not_awaited()
        for role,expected in (("finance","admin:finance"),("marketer","admin:marketing")):
            await admin_access.assign_role(1,42,role,"role-"+role,owner_id=1)
            staff,_=self.actor(42)
            await admin_ui.menu(staff,42,self.admins,1)
            buttons=[b.callback_data for row in staff.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
            self.assertEqual(buttons,[expected])

    async def test_admin_catalog_uses_existing_ten_groups_and_43_types_without_changes(self):
        import equipment_catalog
        owner,state=self.actor(1)
        await admin_ui.admin_action(self.callback(owner,"admin:catalog:groups"),self.admins,state,service_owner_id=1)
        buttons=owner.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([b.text for row in buttons[:-1] for b in row],[name for name,_ in equipment_catalog.GROUPS])
        for index,(name,items) in enumerate(equipment_catalog.GROUPS):
            await admin_ui.admin_action(self.callback(owner,f"admin:catalog:{index}"),self.admins,state,service_owner_id=1)
            self.assertEqual(owner.answer.await_args.args[0],name+"\n\n"+"\n".join(items))
        self.assertEqual(sum(len(items) for _,items in equipment_catalog.GROUPS),43)

    async def test_transaction_rechecks_access_after_stale_precheck_without_losing_forms(self):
        await bot.SQLiteDraftStorage(str(self.database)).init_schema()
        person,state=self.actor(42)
        person.from_user.full_name="Тест"
        person.from_user.username=None
        await bot.user_access.change(1,42,["full"],"Позднее ограничение","atomic-restriction",expected_revision=0,owner_id=1)
        lead_data={"phone":"+79991234567","draft_id":"restricted-lead","equipment":"автокран"}
        await state.set_state(bot.LeadForm.review)
        await state.set_data(lead_data)
        with patch.object(bot.user_access,"allowed",new_callable=AsyncMock,return_value=True):
            await bot.submit_lead(person,person.from_user,state,self.telegram,lead_data)
        self.assertEqual(await state.get_state(),bot.LeadForm.review.state)
        async with aiosqlite.connect(self.database) as db:
            self.assertEqual((await (await db.execute("SELECT count(*) FROM leads")).fetchone())[0],0)
            await db.execute("INSERT INTO leads(customer_id,customer_name,equipment,place,work_date,details,phone,status,created_at) "
                             "VALUES (20,'Заказчик','автокран','Москва','завтра','','+79991234567','published','2026-11-11')")
            await db.commit()
        offer_data={"lead_id":1,"price":"28000","comment":"Без комментария","phone":"+79991234567"}
        await state.set_state(bot.OfferForm.review)
        await state.set_data(offer_data)
        with patch.object(bot.user_access,"allowed",new_callable=AsyncMock,return_value=True):
            await bot.send_offer(person,person.from_user,state,self.telegram)
        self.assertEqual(await state.get_state(),bot.OfferForm.review.state)
        self.assertEqual((await state.get_data())["price"],"28000")
        async with aiosqlite.connect(self.database) as db:
            self.assertEqual((await (await db.execute("SELECT count(*) FROM offers")).fetchone())[0],0)
        self.telegram.send_message.assert_not_awaited()

    async def test_administrator_lead_filters_are_personal_and_keep_publishing_active(self):
        async with aiosqlite.connect(self.database) as db:
            for status in ('new','publishing','published','closed','rejected','archived'):
                await db.execute("INSERT INTO leads(customer_id,customer_name,equipment,place,work_date,details,phone,status,created_at) "
                                 "VALUES (20,'Заказчик','автокран','Москва','завтра','','+79991234567',?,'2026-11-11')",(status,))
            await db.commit()
        self.assertEqual([r["id"] for r in await admin_reports.administrator_leads(1,'active')],[3,2,1])
        self.assertEqual([r["id"] for r in await admin_reports.administrator_leads(1,'closed')],[5,4])
        self.assertEqual(len(await admin_reports.administrator_leads(1,'all')),6)
        await admin_access.mark_lead_view(1,2)
        self.assertEqual([r["id"] for r in await admin_reports.administrator_leads(1,'new')],[3,1])
        self.assertEqual([r["id"] for r in await admin_reports.administrator_leads(2,'new')],[3,2,1])
        self.assertEqual([r["id"] for r in await admin_reports.administrator_leads(1,'all',before=4)],[3,2,1])
        owner,state=self.actor(1)
        await admin_ui.admin_action(self.callback(owner,'admin:leads:new:0'),self.admins,state,service_owner_id=1)
        markup=owner.answer.await_args.kwargs['reply_markup']
        self.assertEqual(markup.inline_keyboard[0][0].callback_data,'lc:open:3:a-new-0')
        lead=await bot.get_lead(3)
        await bot.show_customer_lead(owner,lead,False,True,admin_back='admin:leads:new:0')
        self.assertEqual(owner.answer.await_args.kwargs['reply_markup'].inline_keyboard[-1][0].callback_data,'admin:leads:new:0')
        await admin_ui.menu(owner,1,self.admins,1)
        labels=[b.text for row in owner.answer.await_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('📋 Заявки · 3 / 2',labels)
        owner.answer.side_effect=TelegramNetworkError(None,'timeout')
        with self.assertRaises(TelegramNetworkError):
            await bot.lead_card_action(self.callback(owner,'lc:open:3:a-new-0'),state,self.telegram)
        self.assertEqual([r['id'] for r in await admin_reports.administrator_leads(1,'new')],[3,1])
        owner.answer.side_effect=None
        await bot.lead_card_action(self.callback(owner,'lc:open:3:a-new-0'),state,self.telegram)
        self.assertEqual([r['id'] for r in await admin_reports.administrator_leads(1,'new')],[1])

    async def test_admin_access_checked_on_every_export(self):
        for user, chat_type in ((20, "private"), (1, "supergroup")):
            message, _ = self.actor(user, chat_type=chat_type)
            callback = self.callback(message, "admin:export")
            await admin_ui.admin_action(callback, self.admins)
            callback.answer.assert_awaited_once_with("Нет доступа.", show_alert=True)
            message.answer_document.assert_not_awaited()
        admin, _ = self.actor(1)
        await admin_ui.admin_action(self.callback(admin, "admin:export"), self.admins)
        self.assertTrue(admin.answer_document.await_args.args[0].filename.endswith(".xlsx"))

    async def test_private_visitors_are_deduplicated_in_admin_and_excel(self):
        visitor, _ = self.actor(99)
        visitor.from_user.is_bot = False
        group_member, _ = self.actor(100, chat_type="group")
        group_member.from_user.is_bot = False
        handler = AsyncMock()
        for event in (visitor, visitor, group_member):
            await bot.track_private_visitor(handler, event, {})

        self.assertEqual(dict(await admin_reports.user_totals()),
                         {"total": 1, "customers": 0, "providers": 0, "both": 0})
        people = load_workbook(BytesIO(await admin_reports.export_xlsx()))["Пользователи"]
        self.assertEqual([row[0] for row in people.iter_rows(min_row=2, values_only=True)], ["99"])

    async def test_stats_separate_events_at_moscow_calendar_boundaries(self):
        before_month = "2026-10-31T23:59:59.999999+03:00"
        month = "2026-11-01T00:00:00+03:00"
        week = "2026-11-08T21:00:00+00:00"
        before_day = "2026-11-09T20:59:59.999999+00:00"
        day = "2026-11-10T00:00:00+03:00"
        future = "2026-11-09T21:00:00.000200+00:00"

        async with aiosqlite.connect(self.database) as db:
            await db.executemany(
                """INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,status,created_at)
                   VALUES (?,'кран','Москва','завтра','','+79991234567','published',?)""",
                [(10, before_month), (20, month), (30, week), (30, before_day), (40, day), (50, future)],
            )
            await db.executemany(
                "INSERT INTO providers VALUES (?,'Тест','трал','Москва','+79991234567',?)",
                [(1, before_month), (2, month), (3, week)],
            )
            await db.executemany(
                """INSERT INTO provider_profiles(telegram_id,display_name,phone,consent_at,updated_at)
                   VALUES (?,'Тест','+79991234567',?,?)""",
                [(user, stamp, stamp) for user, stamp in [(1, day), (4, day), (5, future)]],
            )
            events = [(1, month), (2, week), (4, day), (5, future)]
            await db.executemany(
                "INSERT INTO lead_lifecycle_events(lead_id,action,created_at) VALUES (?,'published',?)",
                events,
            )
            await db.executemany(
                """INSERT INTO offers(lead_id,provider_id,provider_name,username,price,comment,phone,created_at)
                   VALUES (?,7,'Тест','','5000','','+79991234567',?)""",
                events,
            )
            await db.commit()

        with self.assertRaises(ValueError):
            await storage.finish_publication(4, 1, 1)
        at = datetime(2026, 11, 9, 21, 0, 0, 100, tzinfo=timezone.utc)
        self.assertEqual([dict(row) for row in await admin_reports.registration_stats(at)], [
            {"providers": 1, "customers": 1, "created": 1, "published": 1, "offers": 1},
            {"providers": 2, "customers": 2, "created": 3, "published": 2, "offers": 2},
            {"providers": 3, "customers": 3, "created": 4, "published": 3, "offers": 3},
        ])

    async def test_excel_prefers_available_contact_data_and_sorts_first_seen_by_time(self):
        earlier = "2026-11-10T01:00:00+03:00"
        later = "2026-11-09 23:00:00"
        async with aiosqlite.connect(self.database) as db:
            await db.execute(
                "INSERT INTO providers VALUES (20,'Перевозчик','трал','Москва','89991234567',?)",
                (earlier,),
            )
            await db.execute(
                """INSERT INTO provider_profiles(telegram_id,display_name,phone,consent_at,updated_at)
                   VALUES (20,' ','?',?,?)""", (later, later),
            )
            await db.executemany(
                """INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,status,created_at)
                   VALUES (20,'трал','Москва','завтра','','','published',?)""",
                [(earlier,), (later,)],
            )
            await db.executemany(
                "INSERT INTO lead_work_details(lead_id,customer_username) VALUES (?,?)",
                [(1, "Actual_handle"), (2, "bad/name")],
            )
            await db.commit()
        users = load_workbook(BytesIO(await admin_reports.export_xlsx()))["Пользователи"]
        headers, row = list(users.values)
        user = dict(zip(headers, row))
        self.assertEqual(user["Имя или компания"], "Перевозчик")
        self.assertEqual(user["Телефон"], "+79991234567")
        self.assertEqual(user["Профиль"], "https://t.me/Actual_handle")
        self.assertEqual(user["Первая заявка"], earlier)
        self.assertEqual(user["Регистрация исполнителя"], earlier)

    async def test_equipment_panel_counts_registered_providers_and_units_without_cards(self):
        admin, _ = self.actor(1)
        await admin_ui.admin_action(self.callback(admin, "admin:equipment:0"), self.admins)
        self.assertEqual(admin.answer.await_args.args[0], "Зарегистрированные исполнители · 0\nЕдиницы техники · 0")
        stamp = "2026-11-09T10:00:00+00:00"
        await provider_registry.register_provider(20, "Современный профиль", "+79991234567", True, db_path=self.database)
        async with aiosqlite.connect(self.database) as db:
            await db.executemany(
                "INSERT INTO providers VALUES (?,?,'трал','Москва','+79991234567',?)",
                [(20, "Старый профиль", stamp), (40, "Только старая регистрация", stamp)],
            )
            await db.executemany(
                """INSERT INTO provider_equipment(provider_id,category,description,region,area,active,created_at)
                   VALUES (20,'автокран',?,'Москва','ЗАО',?,?)""",
                [("Первая машина", 1, stamp), ("Вторая машина", 0, stamp)],
            )
            await db.execute(
                """INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,status,created_at)
                   VALUES (70,'кран','Москва','завтра','','+79991234567','published',?)""",
                (stamp,),
            )
            await db.commit()
        await storage.save_offer(1, 50, "Только отклик", "", "15000", "", "+79991234567")

        admin.answer.reset_mock()
        await admin_ui.admin_action(self.callback(admin, "admin:equipment:0"), self.admins)
        admin.answer.assert_awaited_once()
        self.assertEqual(admin.answer.await_args.args[0], "Зарегистрированные исполнители · 2\nЕдиницы техники · 2")
        buttons = admin.answer.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual([button.callback_data for row in buttons for button in row], ["admin:catalog:groups", "admin:home"])
        self.assertEqual((await admin_reports.user_totals())["providers"], 3)

    async def test_report_counts_and_excel_use_existing_records(self):
        stamp = "2026-11-09T10:00:00+00:00"
        async with aiosqlite.connect(self.database) as db:
            await db.executemany(
                "INSERT INTO leads(customer_id,equipment,place,work_date,details,phone,status,created_at) VALUES (?,'кран','Москва','завтра','','+79991234567',?,?)",
                [(20, "published", stamp), (30, "rejected", stamp), (20, "published", stamp)],
            )
            await db.execute(
                "INSERT INTO provider_profiles(telegram_id,display_name,phone,consent_at,updated_at) VALUES (20,?,'+79991234567',?,?)",
                ("=HYPERLINK(1)", stamp, stamp),
            )
            await db.executemany(
                "INSERT INTO providers VALUES (?,?,'трал','Московская область','+79991234567',?)",
                [(20, "Дубликат профиля", stamp), (40, "Перевозчик", "2026-11-01T10:00:00+00:00")],
            )
            await db.execute(
                "INSERT INTO provider_equipment(provider_id,category,description,region,area,created_at) VALUES (20,'автокран','30 т','Москва','ЗАО',?)",
                (stamp,),
            )
            await db.commit()
        offer = await storage.save_offer(1, 50, "Исполнитель", "", "15000", "Подача включена",
                                         "+79991234567", note="Пропуск оформлен")
        await storage.mark_offer(offer, "sent")
        self.assertTrue(await bot.lead_journey.change_lifecycle(storage.DB_PATH, 1, 20, offer_id=offer))
        await storage.save_offer(1, 51, "Другой исполнитель", "", "16000", "", "+79991234567")
        await storage.save_offer(3, 20, "Кран", "", "20000", "", "+79991234567")
        async with aiosqlite.connect(self.database) as db:
            await db.execute("UPDATE leads SET status='archived' WHERE id=3")
            await db.commit()
        self.assertEqual(dict(await admin_reports.user_totals()), {"total": 5, "customers": 2, "providers": 4, "both": 1})
        registrations = await admin_reports.registration_stats(datetime(2026, 11, 9, 15, tzinfo=timezone.utc))
        self.assertEqual([row["providers"] for row in registrations], [1, 1, 2])
        self.assertEqual([row["customers"] for row in registrations], [2, 2, 2])
        leads, offers, delivery, results = await admin_reports.lead_and_offer_counts()
        self.assertEqual(admin_reports.classify(leads), {"Активные": 1, "Закрытые": 1, "Архивные": 1, "Прочие": 0})
        self.assertEqual(admin_reports.classify(offers)["Архивные"], 1)
        self.assertEqual({row["status"]: row["n"] for row in delivery}, {"pending": 2, "sent": 1})
        self.assertEqual({row["status"]: row["n"] for row in results}, {"Выбран": 1, "Заявка в архиве": 1, "Подан": 1})
        admin, _ = self.actor(1)
        await admin_ui.admin_action(self.callback(admin, "admin:records"), self.admins)
        self.assertIn("Результаты откликов\nВыбран 1 · Заявка в архиве 1 · Подан 1", admin.answer.await_args.args[0])
        self.assertIn("Доставка откликов", admin.answer.await_args.args[0])
        performer, _ = self.actor(20)
        await bot.show_my_offers(performer, 20)
        self.assertIn("Статус: Заявка в архиве", performer.answer.await_args.args[0])
        self.assertIn("Доставка: доставка ожидается", performer.answer.await_args.args[0])
        equipment = await admin_reports.equipment_page(0)
        self.assertEqual(len(equipment), 3)
        self.assertIn("Москва, ЗАО", [item["location"] for item in equipment])
        workbook = load_workbook(BytesIO(await admin_reports.export_xlsx()))
        self.assertEqual(workbook.sheetnames, ["Пользователи", "Техника", "Заявки", "Отклики",
                                               "Обращения", "История статусов и закрытия"])
        headers, submitted, alternate, legacy = list(workbook["Отклики"].values)
        self.assertEqual(submitted[headers.index("Условия")], "Подача включена")
        self.assertEqual(submitted[headers.index("Комментарий")], "Пропуск оформлен")
        self.assertIsNone(legacy[headers.index("Комментарий")])
        self.assertEqual(submitted[headers.index("Результат")], "Выбран")
        self.assertEqual(alternate[headers.index("Результат")], "Подан")
        self.assertEqual(legacy[headers.index("Результат")], "Заявка в архиве")
        people = workbook["Пользователи"]
        self.assertEqual((people["A2"].value, people["A2"].data_type), ("20", "s"))
        self.assertEqual((people["B2"].value, people["B2"].data_type), ("=HYPERLINK(1)", "s"))
        self.assertEqual(workbook["Техника"].max_row, 4)
        self.assertIn("+79991234567", [cell.value for sheet in workbook for row in sheet for cell in row])

    async def test_support_group_gets_only_metadata_and_unknown_members_trigger_fallback(self):
        telegram = self.telegram
        telegram.get_chat = AsyncMock(return_value=Record(type="supergroup", username=None, title="Поддержка"))
        telegram.get_chat_member = AsyncMock(return_value=Record(status="administrator"))
        telegram.get_chat_member_count = AsyncMock(return_value=3)
        await support_ui.notify_admins(telegram, self.admins, -10099, 42, "help")
        telegram.send_message.assert_awaited_once()
        self.assertEqual(telegram.send_message.await_args.args[0], -10099)
        self.assertIn("/support 42", telegram.send_message.await_args.args[1])
        telegram.send_message.reset_mock()
        telegram.get_chat_member_count.return_value = 4
        await support_ui.notify_admins(telegram, self.admins, -10099, 42, "help")
        self.assertEqual([call.args[0] for call in telegram.send_message.await_args_list], [1, 2])
