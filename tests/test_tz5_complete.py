"""Integration coverage against an isolated copy of SQLite and fake Telegram actors."""
import os
from contextlib import redirect_stdout
from io import StringIO
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("LEADS_CHANNEL_ID", "-100123")

import bot
import equipment_catalog
import lead_journey
import provider_registry
import provider_ui
import render_test_backup
import storage
import support_storage


class CompleteTZ5Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "test.sqlite"
        patcher = patch.object(storage, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        await bot.init_db()
        await bot.SQLiteDraftStorage(str(self.path)).init_schema()
        self.customer = SimpleNamespace(id=12, full_name="Заказчик", username="customer_test")

    async def make_lead(self, kind="КДМ", region="Московская область", area="Химки"):
        return (await bot.save_lead_from_draft(self.customer, "+79990000001", {
            "equipment": kind, "region": region, "area": area,
            "place": "Учебный адрес", "work_date": "завтра 09:00", "schedule": "2 смены",
            "map_link": "https://example.test/map", "payment_method": bot.payment_preferences.VAT,
            "details": "Площадка, телефон 8 (999) 123-45-67 #Контакт",
        }))[0]

    async def publish(self, lead_id):
        self.assertTrue(await storage.transition(lead_id, "new", "publishing"))
        await storage.finish_publication(lead_id, -100123, lead_id)

    async def test_customer_card_hides_offers_keeps_status_close_and_legacy_history(self):
        lead_id = await self.make_lead()
        await self.publish(lead_id)
        offer_id = await storage.save_offer(lead_id, 21, 'Исполнитель', '', '28000', 'Смена', '+79990000002')
        message = SimpleNamespace(answer=AsyncMock())
        lead = dict(await bot.get_lead(lead_id))
        for status in ('new', 'published', 'closed'):
            with self.subTest(status=status):
                await bot.show_customer_lead(message, {**lead, 'status': status}, True, False)
                call = message.answer.await_args
                buttons = [b for row in call.kwargs['reply_markup'].inline_keyboard for b in row]
                self.assertNotIn('Отклики', [b.text for b in buttons])
                self.assertFalse(any((b.callback_data or '').startswith('lc:offers:') for b in buttons))
                self.assertIn(f'lc:repeat:{lead_id}', [b.callback_data for b in buttons])
                self.assertIn('lc:list:0', [b.callback_data for b in buttons])
                self.assertEqual('Закрыть заявку' in [b.text for b in buttons], status == 'published')
                self.assertIn(bot.LEAD_STATUS[status], call.args[0])
        message.answer.reset_mock()
        await bot.show_customer_offers(message, lead, 2**63 - 1)
        self.assertIn('28000', message.answer.await_args_list[0].args[0])
        self.assertTrue(await storage.has_offer(lead_id, 21))
        self.assertEqual(await storage.offer_phone_for_customer(offer_id, 12), '+79990000002')
        self.assertTrue(bot.respond_button(lead_id).url.endswith(f'?start=contact_{lead_id}'))

    async def test_provider_primary_menus_hide_offer_actions_without_changing_saved_drafts(self):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute("INSERT INTO providers VALUES (21,'Прежний профиль','кран','Москва','+79990000002','now')")
            await db.commit()
        for actor, registered, enabled in ((22, False, True), (21, False, True), (21, True, True), (21, True, False)):
            with self.subTest(actor=actor, registered=registered, enabled=enabled):
                if registered:
                    await provider_registry.register_provider(actor, 'Исполнитель', '+79990000002', True, str(self.path))
                    await provider_registry.set_notifications(actor, enabled, str(self.path))
                state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=actor, user_id=actor))
                legacy = bot.draft_slot(state, 'offer')
                await legacy.set_state(bot.OfferForm.price)
                await legacy.set_data({'lead_id': 42, 'price': '28000'})
                keyboard = await provider_ui.executor_keyboard(actor, state)
                labels = [b.text for row in keyboard.keyboard for b in row]
                self.assertNotIn(provider_ui.OFFER_DRAFT, labels)
                self.assertNotIn(provider_ui.MY_OFFERS, labels)
                self.assertIn(provider_ui.HOME, labels)
                if registered:
                    self.assertIn(provider_ui.MY_TECH, labels)
                    self.assertIn(provider_ui.NOTIFICATIONS_ON if enabled else provider_ui.NOTIFICATIONS_OFF, labels)
                else:
                    self.assertIn(provider_ui.REGISTER_PROVIDER, labels)
                self.assertEqual(await legacy.get_state(), bot.OfferForm.price.state)
                self.assertEqual(await legacy.get_data(), {'lead_id': 42, 'price': '28000'})
        self.assertNotIn(provider_ui.MY_OFFERS, [b.text for row in provider_ui.provider_tools_keyboard().keyboard for b in row])

    async def test_both_roles_show_the_same_catalog_and_return_controls(self):
        states = MemoryStorage()
        buyer = FSMContext(states, StorageKey(bot_id=1, chat_id=12, user_id=12))
        provider = FSMContext(states, StorageKey(bot_id=1, chat_id=21, user_id=21))

        def actor(user_id):
            return SimpleNamespace(chat=SimpleNamespace(id=user_id),
                                   answer=AsyncMock(return_value=SimpleNamespace(message_id=101)),
                                   bot=SimpleNamespace(edit_message_reply_markup=AsyncMock()))

        customer_chat = actor(12)
        provider_chat = actor(21)
        await bot.show_lead_step(customer_chat, buyer, bot.LeadForm.equipment)
        await provider_ui.machine_catalog(provider_chat, provider)

        def keyboard(chat):
            rows = chat.answer.await_args.kwargs["reply_markup"].inline_keyboard
            return [[button.text for button in row] for row in rows]

        self.assertEqual(keyboard(customer_chat)[:-1], keyboard(provider_chat))
        for index, (group, labels) in enumerate(equipment_catalog.GROUPS):
            with self.subTest(group=group):
                await bot.show_lead_step(customer_chat, buyer, bot.LeadForm.equipment_type,
                                         equipment_group=index)
                await provider_ui.machine_catalog(provider_chat, provider, index)
                customer_rows = keyboard(customer_chat)
                provider_rows = keyboard(provider_chat)
                self.assertEqual(customer_rows[:-1], provider_rows[:-1])
                self.assertEqual(len([button for row in provider_rows[:-1] for button in row]), len(labels))
                self.assertTrue(all(1 <= len(row) <= 2 for row in provider_rows))
                self.assertEqual(customer_rows[-1][0], "⬅️ К категориям")
                self.assertEqual(provider_rows[-1], ["⬅️ К категориям"])

    async def test_approved_catalog_and_symmetric_matching(self):
        for kind in ("КДМ", "автогудронатор", "поливомоечная машина", "каток"):
            self.assertIn(kind, provider_registry.CATEGORIES)
        eligible = 21
        await provider_registry.register_provider(eligible, "Исполнитель", "89990000002", True, str(self.path))
        for kind in ("КДМ", "каток"):
            await provider_registry.add_equipment(eligible, kind, "Тест", "Московская область", "  ХИМКИ  ", str(self.path))
        matching = await self.make_lead()
        legacy_match = await self.make_lead("дорожный каток")
        other_region = await self.make_lead(region="Москва")
        other_kind = await self.make_lead("автокран")
        await self.make_lead()  # remains under moderation
        for lead_id in (matching, legacy_match, other_region, other_kind):
            await self.publish(lead_id)
        self.assertEqual(
            await provider_registry.matching_providers("КДМ", "Московская область", "Химки", str(self.path)),
            [eligible],
        )
        await provider_registry.set_notifications(eligible, False, str(self.path))
        self.assertEqual(
            await provider_registry.matching_providers("КДМ", "Московская область", "Химки", str(self.path)), [],
        )
        message = SimpleNamespace(answer=AsyncMock())
        await bot.show_matching_leads(message, eligible)
        self.assertEqual(len(message.answer.await_args_list), 2)
        shown = "\n".join(call.args[0] for call in message.answer.await_args_list)
        for lead_id in (matching, legacy_match):
            self.assertIn(f"Заявка №{lead_id}", shown)
        for lead_id in (other_region, other_kind):
            self.assertNotIn(f"Заявка №{lead_id}", shown)

    async def test_saved_preferences_drive_notification_and_legacy_list_without_duplicates(self):
        from dataclasses import replace
        await provider_registry.register_provider(21, "Подписчик", "+79990000002", True, str(self.path))
        await provider_registry.save_alert_preferences(
            21, ["КДМ", "автокран"], ["Москва", "Московская область"], 0, str(self.path),
        )
        matching = await self.make_lead()
        other_kind = await self.make_lead("самосвал")
        await self.publish(matching)
        await self.publish(other_kind)
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "settings", replace(bot.settings, test_notification_provider_ids=frozenset({21}))):
            await bot.notify_test_providers(telegram, await bot.get_lead(matching))
            await bot.notify_test_providers(telegram, await bot.get_lead(matching))
            await bot.notify_test_providers(telegram, await bot.get_lead(other_kind))
        telegram.send_message.assert_awaited_once()
        self.assertEqual(telegram.send_message.await_args.args[0], 21)
        self.assertIn(f"№{matching}", telegram.send_message.await_args.args[1])
        self.assertNotIn("+79990000001", telegram.send_message.await_args.args[1])
        message = SimpleNamespace(answer=AsyncMock())
        await bot.show_matching_leads(message, 21)
        self.assertEqual(len(message.answer.await_args_list), 1)
        self.assertIn(f"№{matching}", message.answer.await_args.args[0])
        await provider_registry.set_notifications(21, False, str(self.path))
        another = await self.make_lead()
        await self.publish(another)
        with patch.object(bot, "settings", replace(bot.settings, test_notification_provider_ids=frozenset({21}))):
            await bot.notify_test_providers(telegram, await bot.get_lead(another))
        self.assertEqual(telegram.send_message.await_count, 1)

    async def test_lifecycle_actor_rights_and_data_are_preserved(self):
        lead_id = await self.make_lead()
        await self.publish(lead_id)
        offer_id = await storage.save_offer(lead_id, 21, "Исполнитель", "", "30000", "Условия", "+79990000002")
        self.assertFalse(await lead_journey.change_lifecycle(str(self.path), lead_id, 22, offer_id=offer_id))
        self.assertTrue(await lead_journey.change_lifecycle(str(self.path), lead_id, 12, offer_id=offer_id))
        self.assertFalse(await lead_journey.change_lifecycle(str(self.path), lead_id, 12, offer_id=offer_id))
        self.assertFalse(await lead_journey.change_lifecycle(str(self.path), lead_id, 22, reason="Чужая заявка"))
        self.assertTrue(await lead_journey.change_lifecycle(str(self.path), lead_id, 1, admin=True, reason="Работа завершена"))
        lead = await bot.get_lead(lead_id)
        self.assertEqual(lead["status"], "closed")
        self.assertEqual(lead["schedule"], "2 смены")
        self.assertTrue(await storage.has_offer(lead_id, 21))
        self.assertIsNone(await storage.save_offer(lead_id, 22, "Другой", "", "1", "", "+79990000003"))
        self.assertIn("+79990000001", bot.lead_text(lead, True))
        public = bot.lead_text(lead)
        self.assertNotIn("+79990000001", public)
        self.assertNotIn("123-45-67", public)
        self.assertNotIn("#Контакт", public)
        self.assertIn(bot.payment_preferences.VAT, public)

    async def test_close_reason_is_optional_but_validated_and_saved_without_outer_spaces(self):
        lead_id = await self.make_lead()
        await self.publish(lead_id)
        for invalid in ("x" * 501, 123):
            with self.subTest(reason=invalid):
                with self.assertRaises(ValueError):
                    await lead_journey.change_lifecycle(str(self.path), lead_id, 12, reason=invalid)
                self.assertEqual((await bot.get_lead(lead_id))["status"], "published")
        self.assertTrue(await lead_journey.change_lifecycle(str(self.path), lead_id, 12,
                                                           reason="  Работы завершены  "))
        async with aiosqlite.connect(self.path) as db:
            self.assertEqual(await (await db.execute(
                "SELECT actor_id,reason FROM lead_lifecycle_events WHERE lead_id=? AND action='closed'", (lead_id,),
            )).fetchone(), (12, "Работы завершены"))

    async def test_support_thread_continues_only_for_owner_and_keeps_status_history(self):
        ticket, _ = await support_storage.create_ticket(12, "help", "initial", "Вопрос", None)
        self.assertFalse(await support_storage.add_requester_message(ticket, 21, "forbidden", "Чужое обращение"))
        self.assertTrue(await support_storage.add_requester_message(ticket, 12, "followup", "Уточнение"))
        self.assertFalse(await support_storage.add_requester_message(ticket, 12, "followup", "Повтор"))
        self.assertTrue(await support_storage.change_status(ticket, "closed", 1))
        self.assertFalse(await support_storage.add_requester_message(ticket, 12, "late", "После закрытия"))
        async with aiosqlite.connect(self.path) as db:
            self.assertEqual(await (await db.execute("SELECT actor_id,status FROM support_status_events")).fetchall(),
                             [(1, "closed")])
        self.assertEqual(len(await support_storage.history(ticket)), 2)

    async def test_backup_restore_preserves_rows_and_detects_change(self):
        lead_id = await self.make_lead()
        backup, digest = render_test_backup.snapshot(self.path)
        candidate = render_test_backup.restore_candidate(backup, digest)
        with patch.object(storage, "DB_PATH", str(candidate)):
            await bot.init_db()
            await bot.init_db()
        self.assertGreaterEqual(render_test_backup.verify_preserved_rows(backup, candidate), 19)
        with sqlite3.connect(candidate) as db:
            db.execute("UPDATE leads SET details='modified' WHERE id=?", (lead_id,))
        with self.assertRaisesRegex(RuntimeError, "pre-existing rows"):
            render_test_backup.verify_preserved_rows(backup, candidate)


    async def test_restore_command_checks_digest_and_compares_rows(self):
        await self.make_lead()
        backup, digest = render_test_backup.snapshot(self.path)
        output = StringIO()
        argv = ["render_test_backup.py", "restore-candidate", "--backup", str(backup), "--sha256"]
        with patch("render_test_start.test_database_path", return_value=self.path):
            with patch("sys.argv", argv + ["0" * 64]):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    render_test_backup.main()
            self.assertEqual(list(backup.parent.glob("restore-candidate-*.sqlite")), [])
            with patch("sys.argv", argv + [digest]), redirect_stdout(output):
                render_test_backup.main()
        candidate = next(backup.parent.glob("restore-candidate-*.sqlite"))
        self.assertIn(f"{candidate} SHA-256 ", output.getvalue())
        self.assertGreaterEqual(render_test_backup.verify_preserved_rows(self.path, candidate), 19)


    async def test_additive_attachments_schema_preserves_legacy_rows_and_excel(self):
        legacy = self.path.with_name("legacy.sqlite")
        with sqlite3.connect(legacy) as db:
            db.executescript("""
                CREATE TABLE leads(id INTEGER PRIMARY KEY, details TEXT);
                CREATE TABLE offers(id INTEGER PRIMARY KEY);
                INSERT INTO leads VALUES (11,'Историческая заявка');
                INSERT INTO offers VALUES (14);
            """)
        snapshot, digest = render_test_backup.snapshot(legacy)
        candidate = render_test_backup.restore_candidate(snapshot, digest)
        for _ in range(2):
            await lead_journey.init_schema(str(candidate))
            self.assertEqual(render_test_backup.verify_preserved_rows(snapshot, candidate), 2)
        self.assertEqual(lead_journey.attachments_text({"details": "Старая запись"}), "")
        lead_id = await self.make_lead()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE lead_attachments SET selected_json=?,custom_text=? WHERE lead_id=?",
                             ('["Вилы"]', "Особое", lead_id))
            await db.commit()
        from openpyxl import load_workbook
        from io import BytesIO
        import admin_reports
        sheet = load_workbook(BytesIO(await admin_reports.export_xlsx()))["Заявки"]
        header, row = list(sheet.values)
        self.assertEqual(row[header.index("Допоборудование")], "Вилы, Особое")

    async def test_expired_submission_has_no_rows_and_keeps_input(self):
        data={'draft_id':'expired-date','work_date':'01.01.2000 09:00','place':'Сохранённый адрес'}
        before=dict(data)
        with self.assertRaises(bot.ExpiredLeadDate):
            await bot.save_lead_from_draft(self.customer,'+79990000001',data)
        self.assertEqual(data,before)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM leads').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM lead_draft_submissions').fetchone()[0],0)

    async def test_existing_submission_stays_idempotent_after_date_expires(self):
        lead_id=await self.make_lead()
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO lead_draft_submissions VALUES (?,?)',('saved-before-expiry',lead_id))
        result=await bot.save_lead_from_draft(self.customer,'+79990000001',{'draft_id':'saved-before-expiry','work_date':'01.01.2000 09:00'})
        self.assertEqual(result,(lead_id,False))

    async def test_repeat_lead_preserves_every_source_field_and_blocks_duplicate_draft(self):
        from unittest.mock import MagicMock
        from aiogram.types import Message
        lead_id=await self.make_lead()
        await self.publish(lead_id)
        source=dict(await bot.get_lead(lead_id))
        state=FSMContext(MemoryStorage(),StorageKey(bot_id=99,chat_id=12,user_id=12))
        message=MagicMock(spec=Message)
        message.chat=SimpleNamespace(id=12,type='private')
        message.answer=AsyncMock(return_value=SimpleNamespace(message_id=51))
        message.bot=SimpleNamespace(edit_message_reply_markup=AsyncMock())
        callback=SimpleNamespace(data=f'lc:repeat:{lead_id}',message=message,from_user=self.customer,answer=AsyncMock())
        await bot.lead_card_action(callback,state,message.bot)
        copied=await state.get_data()
        self.assertEqual(await state.get_state(),bot.LeadForm.review.state)
        for key in ('equipment','equipment_name','region','area','place','map_link','work_date','schedule','details','payment_method','phone'):
            with self.subTest(field=key): self.assertEqual(copied[key],source[key])
        self.assertTrue(copied['draft_id'])
        await bot.lead_card_action(callback,state,message.bot)
        self.assertEqual((await state.get_data())['draft_id'],copied['draft_id'])
        self.assertEqual(dict(await bot.get_lead(lead_id)),source)
