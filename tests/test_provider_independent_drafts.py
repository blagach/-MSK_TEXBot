"""Subscriptions and inventory coexist on a restored isolated SQLite."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message

os.environ.setdefault("BOT_TOKEN", "99:TEST")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("LEADS_CHANNEL_ID", "-100123")

import bot
import provider_registry as registry
import provider_ui as ui
import storage
from render_test_backup import backup_database, verify_preserved_rows


class IndependentProviderDraftTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "source.sqlite"
        patcher = patch.object(storage, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        await bot.init_db()
        await bot.SQLiteDraftStorage(str(self.path)).init_schema()
        await registry.register_provider(101, "TEST", "+79991234567", True, str(self.path))
        await registry.save_alert_preferences(101, ["автокран"], ["Москва"], 0, str(self.path))
        await registry.add_equipment(101, "автокран", "TEST saved card", "Москва", "весь регион", str(self.path))
        self.message = MagicMock(spec=Message)
        self.message.from_user = Record(id=101)
        self.message.chat = Record(id=101, type="private")
        self.message.message_id = 500
        self.message.bot = Record(edit_message_reply_markup=AsyncMock())
        self.message.answer = AsyncMock(return_value=Record(message_id=500))

    def state(self, path=None):
        return FSMContext(bot.SQLiteDraftStorage(str(path or storage.DB_PATH)),
                          StorageKey(bot_id=99, chat_id=101, user_id=101))

    def rows(self, path):
        with sqlite3.connect(path) as db:
            schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            return schema, {name: db.execute('SELECT * FROM "' + name + '"').fetchall()
                            for kind, name, _ in schema if kind == "table"}

    async def click(self, state, action):
        callback = Record(message=self.message, from_user=self.message.from_user,
                          data="provider_alert:" + action, answer=AsyncMock())
        await ui.alert_preference_action(callback, state)

    async def test_every_inventory_step_switches_and_recovers_both_drafts_after_restart(self):
        original = self.rows(self.path)
        for step in ui.AddMachine.__all_states__:
            with self.subTest(step=step.state):
                restored = self.path.with_name(step.state.replace(":", "-") + ".sqlite")
                backup_database(self.path, restored)
                verify_preserved_rows(self.path, restored)
                with patch.object(storage, "DB_PATH", str(restored)):
                    await bot.init_db()
                    self.assertEqual(self.rows(restored), original)
                    state = self.state()
                    values = {"category": "автокран", "description": "TEST unfinished card",
                              "region": "Москва", "area": "ЦАО",
                              "machine_group": None if step == ui.AddMachine.category else 2,
                              "edit_machine": 1, "machine_panel": 400}
                    await state.storage.checkpoint(state.key, step, values)
                    lead = bot.draft_slot(state, "lead")
                    await lead.set_state(bot.LeadForm.region)
                    await lead.set_data({"equipment": "манипулятор"})
                    # Both an active card and a card paused through /menu must work.
                    if step in (ui.AddMachine.category, ui.AddMachine.review):
                        await bot.pause_form(self.message, state)
                    self.message.text = ui.MY_TECH
                    await ui.router.propagate_event("message", self.message, state=state,
                                                    raw_state=await state.get_state())
                    self.assertEqual(await state.get_state(), ui.AlertPreferences.select.state)
                    card = bot.draft_slot(state, "provider_action")
                    self.assertEqual(await card.get_state(), step.state)
                    self.assertEqual(await card.get_data(), values)
                    # A delayed inventory callback cannot act on the subscription form.
                    stale = Record(message=self.message, from_user=self.message.from_user,
                                   data="machine_catalog:0", answer=AsyncMock())
                    await ui.machine_catalog_action(stale, state)
                    self.assertEqual(await card.get_data(), values)
                    self.assertEqual(await state.get_state(), ui.AlertPreferences.select.state)
                    await self.click(state, "group:2")
                    await self.click(state, "type:1")
                    await bot.pause_form(self.message, state)
                    state = self.state()
                    actions = dict(await bot.available_paused_actions(state))
                    self.assertIn("provider_action", actions)
                    self.assertIn("provider_alert", actions)
                    await bot.resume_auxiliary_kind(self.message, state, "provider_action", 101)
                    self.assertEqual(await state.get_state(), step.state)
                    for key, value in values.items():
                        if key != "machine_panel":
                            self.assertEqual((await state.get_data())[key], value)
                    # Re-enter subscriptions directly from the restored inventory step.
                    self.message.text = ui.MY_REGIONS
                    await ui.begin_alert_preferences(self.message, state)
                    self.assertEqual((await state.get_data())["alert_categories"], ["автокран", "манипулятор"])
                    await self.click(state, "both")
                    await self.click(state, "save")
                    self.assertEqual((await registry.alert_preferences(101, str(restored)))["categories"],
                                     ["автокран", "манипулятор"])
                    await bot.resume_auxiliary_kind(self.message, state, "provider_action", 101)
                    self.assertEqual(await state.get_state(), step.state)
                    self.assertEqual((await state.get_data())["description"], values["description"])
                    self.assertEqual(await lead.get_data(), {"equipment": "манипулятор"})
                    schema, rows = self.rows(restored)
                    self.assertEqual(schema, original[0])
                    for table in original[1]:
                        if table not in {"form_drafts", "provider_alert_preferences"}:
                            self.assertEqual(rows[table], original[1][table], table)
                    with sqlite3.connect(restored) as db:
                        self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                        self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(self.rows(self.path), original)

    async def test_legacy_subscription_draft_moves_without_loss_and_collision_is_preserved(self):
        state = self.state()
        legacy = bot.draft_slot(state, "provider_action")
        values = {"alert_categories": ["автокран", "манипулятор"], "alert_regions": ["Москва"],
                  "alert_kind": "categories", "alert_group": None, "alert_revision": 1}
        await legacy.storage.checkpoint(legacy.key, ui.AlertPreferences.select, values)
        self.message.text = ui.MY_TECH
        await ui.begin_alert_preferences(self.message, state)
        self.assertIsNone(await legacy.get_state())
        self.assertEqual((await state.get_data())["alert_categories"], values["alert_categories"])
        await bot.pause_form(self.message, state)
        self.assertEqual(await bot.draft_slot(state, "provider_alert").get_state(), ui.AlertPreferences.select.state)
        await legacy.storage.checkpoint(legacy.key, ui.AlertPreferences.select, {**values, "alert_regions": ["Московская область"]})
        before = self.rows(self.path)
        await ui.begin_alert_preferences(self.message, state)
        self.assertEqual(self.rows(self.path), before)
        self.assertIn("оставлены без изменений", self.message.answer.await_args.args[0])
