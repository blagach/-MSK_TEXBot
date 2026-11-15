"""Provider-only labels and subscription persistence with fake Telegram."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

os.environ.setdefault("BOT_TOKEN", "123456:TEST")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("LEADS_CHANNEL_ID", "-100123")

import bot
import equipment_catalog as catalog
import provider_registry as registry
import provider_ui as ui
import storage


class ProviderCategoryUITests(unittest.IsolatedAsyncioTestCase):
    async def test_multiselect_save_reopen_without_inventory_preserves_rows_and_schema(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            storage, "DB_PATH", str(Path(temporary) / "isolated.sqlite")
        ):
            await bot.init_db()
            await registry.register_provider(101, "TEST", "+79991234567", True, storage.DB_PATH)
            await registry.save_alert_preferences(101, ["мини-экскаватор"], ["Москва"], 0, storage.DB_PATH)
            def snapshot():
                with sqlite3.connect(storage.DB_PATH) as db:
                    schema = db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
                    return schema, {name: db.execute('SELECT * FROM "' + name + '"').fetchall()
                                    for kind, name, _ in schema if kind == "table"}
            before_schema, before_rows = snapshot()
            message = MagicMock(spec=Message)
            message.from_user = Record(id=101)
            message.chat = Record(id=101, type="private")
            message.message_id = 10
            message.text = ui.MY_TECH
            message.bot = Record(edit_message_reply_markup=AsyncMock())
            message.answer = AsyncMock(return_value=Record(message_id=10))
            state = FSMContext(MemoryStorage(), StorageKey(bot_id=99, chat_id=101, user_id=101))
            def screen():
                call = message.answer.await_args
                return call.args[0], [b for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
            async def click(action):
                await ui.alert_preference_action(Record(message=message, from_user=message.from_user,
                                                       data="provider_alert:" + action, answer=AsyncMock()), state)
            await ui.begin_alert_preferences(message, state)
            text, buttons = screen()
            self.assertEqual(buttons[0].text, "✅ 🚜 Экскаваторы")
            self.assertEqual(buttons[1].text, "🚜 Погрузчики")
            await click("group:0")
            text, buttons = screen()
            self.assertEqual(buttons[0].text, "✅ Мини-экскаватор")
            self.assertEqual(buttons[1].text, "Гусеничный экскаватор")
            self.assertEqual([b.callback_data for b in buttons[:5]],
                             [f"provider_alert:type:{i}" for i in range(5)])
            await click("type:1")
            text, buttons = screen()
            self.assertEqual(buttons[1].text, "✅ Гусеничный экскаватор")
            self.assertIn("• Мини-экскаватор\n• Гусеничный экскаватор", text)
            await click("type:0")
            self.assertNotIn("• Мини-экскаватор", screen()[0])
            await click("type:0")
            await click("back")
            self.assertEqual(screen()[1][0].text, "✅ 🚜 Экскаваторы")
            await click("save")
            # Recreate the UI/FSM session: read saved subscriptions, not previous memory.
            state = FSMContext(MemoryStorage(), state.key)
            await ui.begin_alert_preferences(message, state)
            await click("group:0")
            self.assertEqual([b.text for b in screen()[1][:2]],
                             ["✅ Мини-экскаватор", "✅ Гусеничный экскаватор"])
            for kind in ("мини-экскаватор", "гусеничный экскаватор"):
                self.assertEqual(await registry.matching_providers(kind, "Москва", "ЦАО", storage.DB_PATH), [101])
                self.assertEqual(await registry.matching_providers(kind, "Московская область", "Химки", storage.DB_PATH), [])
            self.assertEqual(await registry.matching_providers("манипулятор", "Москва", "ЦАО", storage.DB_PATH), [])
            after_schema, after_rows = snapshot()
            self.assertEqual(before_schema, after_schema)
            self.assertEqual(after_rows["provider_equipment"], [])
            for name in before_rows:
                if name != "provider_alert_preferences":
                    self.assertEqual(before_rows[name], after_rows[name], name)
            self.assertEqual(after_rows["provider_alert_preferences"][:-1], before_rows["provider_alert_preferences"])
            with sqlite3.connect(storage.DB_PATH) as db:
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
