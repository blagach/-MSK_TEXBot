"""Production/test delivery boundaries, isolated SQLite and fake Telegram only."""
import asyncio
from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as Record
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("LEADS_CHANNEL_ID", "-100123")
import bot
import provider_dispatch as dispatch
import provider_registry as registry
import storage


class ProviderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Retain the isolated fixtures for diagnosis; never delete live data.
        self.folder = Path(tempfile.mkdtemp(prefix="msk-delivery-test-"))
        self.database = self.folder / "bot.db"
        self.patch = patch.object(storage, "DB_PATH", str(self.database))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.config = patch.object(bot, "settings", replace(bot.settings,
            bot_username="MSK_TEXBot", leads_channel_id=-100123,
            test_notification_provider_ids=frozenset()))
        self.config.start()
        self.addCleanup(self.config.stop)
        await bot.init_db()
        self.telegram = Record(id=99, send_message=AsyncMock(),
            get_me=AsyncMock(return_value=Record(id=99, username="MSK_TEXBot", is_bot=True)),
            get_chat=AsyncMock(return_value=Record(id=-100123, type="channel", username="zayavki_specteh_msk")),
            get_chat_member=AsyncMock(return_value=Record(status="administrator", can_post_messages=True)))
        self.sleep = patch.object(bot.asyncio, "sleep", new_callable=AsyncMock)
        self.pause = self.sleep.start()
        self.addCleanup(self.sleep.stop)
        for actor in (101, 102, 103, 104, 105):
            await registry.register_provider(actor, "Учебный исполнитель", "+79990000001", True, str(self.database))
        for actor, categories, regions in (
            (101, ["автокран", "самосвал"], ["Москва", "Московская область"]),
            (102, ["самосвал"], ["Москва"]),
            (103, ["автокран"], ["Московская область"]),
            (104, ["автокран"], ["Москва"]),
            (105, ["автокран"], ["Москва"]),
        ):
            await registry.save_alert_preferences(actor, categories, regions, 0, str(self.database))
        await registry.set_notifications(104, False, str(self.database))
        await registry.set_blocked(105, True, admin_id=1, admin_ids={1}, db_path=str(self.database))

    async def arm(self):
        scope = await dispatch.verify_production_scope(self.telegram, "MSK_TEXBot", -100123)
        self.telegram._provider_notification_scope = scope

    async def lead(self, category="автокран", region="Москва", channel=-100123):
        lead_id, _ = await bot.save_lead_from_draft(Record(id=10, full_name="Учебный заказчик", username=None),
            "+79990000002", {"equipment":category,"region":region,"area":"весь регион",
                "place":"Учебная площадка","work_date":"25.12.2099 09:00","details":"Учебные условия",
                "payment_method":bot.payment_preferences.ANY})
        await storage.transition(lead_id, "new", "publishing")
        await storage.finish_publication(lead_id, channel, lead_id)
        return await bot.get_lead(lead_id)

    def rows(self, database=None):
        with sqlite3.connect(database or self.database) as c:
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {n: sorted(c.execute('SELECT * FROM "'+n+'"').fetchall(),key=repr) for n in names}

    async def test_production_identity_channel_and_test_conflicts_fail_closed(self):
        await self.arm()
        self.assertIsInstance(self.telegram._provider_notification_scope, dispatch.ProductionNotificationScope)
        for kwargs in ({"restricted_ids":{101}}, {"test_configured":True}):
            with self.assertRaises(RuntimeError):
                await dispatch.verify_production_scope(self.telegram, "MSK_TEXBot", -100123, **kwargs)
        for target, attribute, wrong in (
            (self.telegram.get_me.return_value,"username","Msktex_test_dmitry_bot"),
            (self.telegram.get_me.return_value,"id",199),
            (self.telegram.get_chat.return_value,"username","demo_channel"),
            (self.telegram.get_chat.return_value,"id",-100999),
            (self.telegram.get_chat.return_value,"type","supergroup"),
            (self.telegram.get_chat_member.return_value,"can_post_messages",False),
        ):
            original = getattr(target, attribute)
            with self.subTest(attribute=attribute,wrong=wrong), patch.object(target,attribute,wrong):
                with self.assertRaises(RuntimeError):
                    await dispatch.verify_production_scope(self.telegram,"MSK_TEXBot",-100123)
            self.assertEqual(getattr(target,attribute),original)
        self.telegram.send_message.assert_not_awaited()

    async def test_production_multiple_types_regions_concurrency_and_unknown_send_never_replay(self):
        await self.arm()
        lead = await self.lead()
        historical = await storage.save_offer(lead["id"], 199, "Учебный", "", "1", "", "+79990000003")
        await storage.mark_offer(historical,"unconfirmed")
        before = self.rows()
        self.telegram.send_message.side_effect = TelegramNetworkError(SendMessage(chat_id=101,text="x"),"timeout after acceptance")
        await asyncio.gather(*(bot.notify_test_providers(self.telegram,lead) for _ in range(2)))
        self.assertEqual(self.telegram.send_message.await_count,1)
        self.telegram.send_message.side_effect = None
        # Another call, as after a restart, must respect the persistent uncertain reservation.
        self.telegram._provider_notification_scope = dispatch.ProductionNotificationScope(99,-100123)
        await bot.notify_test_providers(self.telegram,lead)
        self.assertEqual(self.telegram.send_message.await_count,1)
        with sqlite3.connect(self.database) as c:
            self.assertEqual(c.execute('SELECT provider_id,status FROM provider_notification_reservations').fetchall(),[(101,"uncertain")])
            self.assertEqual(c.execute('SELECT delivery_status FROM offers WHERE id=?',(historical,)).fetchone(),("unconfirmed",))
        after = self.rows()
        for table in before:
            if table != "provider_notification_reservations":
                self.assertEqual(after[table],before[table],table)
        for category,region,expected in (("самосвал","Москва",[101,102]),("автокран","Московская область",[101,103])):
            self.telegram.send_message.reset_mock()
            item = await self.lead(category,region)
            await bot.notify_test_providers(self.telegram,item)
            self.assertEqual([c.args[0] for c in self.telegram.send_message.await_args_list],expected)
            await bot.notify_test_providers(self.telegram,item)
            self.assertEqual(self.telegram.send_message.await_count,len(expected))
        self.assertIn(unittest.mock.call(1.05), self.pause.await_args_list)

    async def test_test_allowlist_and_publication_origin_cannot_leak_to_production(self):
        lead = await self.lead(category="самосвал")
        await bot.notify_test_providers(self.telegram,lead)  # No verified scope, no production fan-out.
        self.telegram.send_message.assert_not_awaited()
        await self.arm()
        with patch.object(bot,"settings",replace(bot.settings,test_notification_provider_ids=frozenset({101}))):
            await bot.notify_test_providers(self.telegram,lead)
        self.assertEqual([c.args[0] for c in self.telegram.send_message.await_args_list],[101])
        self.telegram.send_message.reset_mock()
        foreign = await self.lead(channel=-100999)
        await bot.notify_test_providers(self.telegram,foreign)
        self.telegram.send_message.assert_not_awaited()
        self.telegram._provider_notification_scope = dispatch.ProductionNotificationScope(199,-100123)
        await bot.notify_test_providers(self.telegram,await self.lead())
        self.telegram.send_message.assert_not_awaited()

    async def test_recheck_opt_out_during_batch_and_explicit_429_retry_only(self):
        await self.arm()
        lead = await self.lead(category="самосвал")
        async def first_send(*args,**kwargs):
            await registry.set_notifications(102,False,str(self.database))
        self.telegram.send_message.side_effect = first_send
        await bot.notify_test_providers(self.telegram,lead)
        self.assertEqual([c.args[0] for c in self.telegram.send_message.await_args_list],[101])
        limited = TelegramRetryAfter(SendMessage(chat_id=101,text="x"),"rejected",retry_after=2)
        self.telegram.send_message.reset_mock()
        self.telegram.send_message.side_effect = [limited, Record(message_id=1)]
        self.assertTrue(await bot.send_provider_notification(self.telegram,101,"x"))
        self.assertEqual(self.telegram.send_message.await_count,2)
        self.assertIn(unittest.mock.call(2),self.pause.await_args_list)
        self.telegram.send_message.reset_mock()
        self.telegram.send_message.side_effect = [limited,limited]
        self.assertFalse(await bot.send_provider_notification(self.telegram,101,"x"))
        self.assertEqual(self.telegram.send_message.await_count,2)

    async def test_backup_restore_reinitialization_preserves_existing_rows_and_schema(self):
        await registry.add_equipment(101,"автокран","Учебная машина","Москва","весь регион",str(self.database))
        backing = bot.SQLiteDraftStorage(str(self.database))
        await backing.init_schema()
        saved = FSMContext(backing,StorageKey(bot_id=99,chat_id=101,user_id=101))
        await saved.set_state(bot.provider_ui.AddMachine.description)
        await saved.set_data({"category":"самосвал","description":"Несохранённая карточка"})
        independent = bot.draft_slot(saved,"lead")
        await independent.set_state(bot.LeadForm.region)
        await independent.set_data({"equipment":"каток"})
        await bot.admin_access.record(1,"compatibility_fixture",target_id=101,result="ok")
        lead = await self.lead()
        offer = await storage.save_offer(lead["id"],199,"Учебный","","1","","+79990000003")
        await storage.mark_offer(offer,"unconfirmed")
        before = self.rows()
        snapshot, restored = self.folder / "snapshot.sqlite", self.folder / "restore.sqlite"
        with sqlite3.connect(self.database.as_uri()+"?mode=ro",uri=True) as source, sqlite3.connect(snapshot) as dest:
            source.backup(dest)
        with sqlite3.connect(snapshot.as_uri()+"?mode=ro",uri=True) as source, sqlite3.connect(restored) as dest:
            source.backup(dest)
        self.assertEqual(self.rows(restored),before)
        with patch.object(storage,"DB_PATH",str(restored)):
            await bot.init_db()
            await bot.init_db()
        self.assertEqual(self.rows(restored),before)
        self.assertEqual(self.rows(),before)
        with sqlite3.connect(restored) as c:
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone(),("ok",))
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    async def test_main_identity_failure_does_not_register_commands_or_start_polling(self):
        with patch.object(bot,"Bot",return_value=self.telegram), patch.object(bot.provider_dispatch,
            "verify_production_scope",new_callable=AsyncMock,side_effect=RuntimeError("wrong identity")), patch.object(
            bot,"create_dispatcher") as dispatcher:
            with self.assertRaisesRegex(RuntimeError,"wrong identity"):
                await bot.main()
            dispatcher.assert_not_called()
        self.telegram.send_message.assert_not_awaited()
