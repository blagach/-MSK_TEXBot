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

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("LEADS_CHANNEL_ID", "-100123")

import bot
import lead_journey
import provider_registry
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
            "place": "Учебный адрес", "work_date": "15 ноября 09:00", "schedule": "2 смены",
            "map_link": "https://example.test/map", "payment_method": bot.payment_preferences.VAT,
            "details": "Площадка, телефон 8 (999) 123-45-67 #Контакт",
        }))[0]

    async def publish(self, lead_id):
        self.assertTrue(await storage.transition(lead_id, "new", "publishing"))
        await storage.finish_publication(lead_id, -100123, lead_id)

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
