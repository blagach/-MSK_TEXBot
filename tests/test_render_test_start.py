import os
import importlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramConflictError

import render_test_start as worker
from render_test_backup import backup_database, verify_small_snapshot
from render_test_guard import IsolationError


CONFIG = {
    "RENDER_SERVICE_ID": worker.TEST_WORKER_ID,
    "TEST_BOT_TOKEN": "123:fake-token",
    "TEST_ADMIN_ID": "101",
    "TEST_CHANNEL_ID": "-100123",
    "TESTER_IDS": "101,102",
}


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.database = Path(self.folder.name) / "bot.db"
        self.telegram = MagicMock()
        self.telegram.get_me = AsyncMock(return_value=SimpleNamespace(
            username=worker.TEST_BOT, is_bot=True,
        ))
        self.telegram.get_webhook_info = AsyncMock(return_value=SimpleNamespace(url=""))
        self.telegram.set_my_commands = AsyncMock()
        self.telegram.set_chat_menu_button = AsyncMock()
        self.bot = MagicMock()
        self.bot.return_value.__aenter__.return_value = self.telegram
        self.dispatcher = MagicMock()
        self.dispatcher.start_polling = AsyncMock()
        self.app = MagicMock()
        self.app.init_db = AsyncMock()
        self.app.create_dispatcher.return_value = self.dispatcher

    async def launch(self, env=None, app=None):
        with (
            patch.dict(os.environ, CONFIG if env is None else env, clear=True),
            patch.object(worker, "Bot", self.bot),
            patch.object(worker, "test_database_path", return_value=self.database),
            patch.object(worker.TelegramIsolation, "check_channel", new_callable=AsyncMock) as check_channel,
            patch.object(worker.importlib, "import_module", return_value=app if app is not None else self.app),
        ):
            await worker.start()
            self.assertEqual(os.environ["SERVICE_OWNER_ID"], CONFIG["TEST_ADMIN_ID"])
            check_channel.assert_awaited_once_with(self.telegram)

    async def test_worker_wires_guard_and_test_database(self):
        await self.launch()
        self.assertEqual(self.app.storage.DB_PATH, str(self.database))
        self.app.init_db.assert_awaited_once()
        self.telegram.session.middleware.assert_called()
        self.dispatcher.update.outer_middleware.assert_called_once()
        commands = self.telegram.set_my_commands.await_args
        self.assertEqual([item.command for item in commands.args[0]], ["start", "menu", "cancel", "support"])
        self.assertEqual(commands.kwargs["scope"].type, "all_private_chats")
        self.assertEqual(self.telegram.set_chat_menu_button.await_args.kwargs["menu_button"].type, "commands")
        self.dispatcher.start_polling.assert_awaited_once_with(
            self.telegram, allowed_updates=["message", "callback_query"], close_bot_session=False,
        )

    async def test_worker_runs_adapted_application_with_its_own_database(self):
        from aiogram import Dispatcher

        with patch.dict(os.environ, {"BOT_TOKEN": "123:fake", "ADMIN_IDS": "101", "LEADS_CHANNEL_ID": "-100123"}):
            app = importlib.import_module("bot")
        with (
            patch.object(app.storage, "DB_PATH", "must-not-use.sqlite"),
            patch.object(Dispatcher, "start_polling", autospec=True) as poll,
        ):
            await self.launch(app=app)
            self.assertEqual(app.storage.DB_PATH, str(self.database))
            poll.assert_awaited_once()
            dispatcher, telegram = poll.await_args.args
            self.assertIs(telegram, self.telegram)
            self.assertIsInstance(dispatcher.fsm.storage, worker.SQLiteDraftStorage)
            self.assertEqual(dispatcher.fsm.storage.db_path, str(self.database))
            self.assertIn(app.router, dispatcher.sub_routers)
            self.assertIn(app.provider_ui.router, dispatcher.sub_routers)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM leads").fetchone(), (0,))
            self.assertEqual(db.execute("SELECT count(*) FROM offers").fetchone(), (0,))

    async def test_worker_rejects_shared_settings_and_unapproved_admin(self):
        for env in (
            {**CONFIG, "BOT_TOKEN": "unexpected"},
            {**CONFIG, "TEST_ADMIN_ID": "999"},
            {**CONFIG, "RENDER_SERVICE_ID": "another-worker"},
            {**CONFIG, "SERVICE_OWNER_ID": "999"},
        ):
            with self.subTest(env=env), self.assertRaises(IsolationError):
                await self.launch(env)
        self.bot.assert_not_called()
        self.app.init_db.assert_not_awaited()
        self.telegram.set_my_commands.assert_not_awaited()

    async def test_worker_rejects_production_token_and_existing_webhook(self):
        self.telegram.get_me.return_value.username = "MSK_TEXBot"
        with self.assertRaises(IsolationError):
            await self.launch()
        self.telegram.get_me.return_value.username = worker.TEST_BOT
        self.telegram.get_webhook_info.return_value.url = "https://example.invalid/active"
        with self.assertRaises(IsolationError):
            await self.launch()
        self.app.init_db.assert_not_awaited()

    async def test_worker_stops_on_polling_conflict(self):
        import asyncio

        async def stop():
            finished.set()

        async def poll(*args, **kwargs):
            middleware = self.telegram.session.middleware.call_args.args[0]
            request = AsyncMock(side_effect=TelegramConflictError(
                method=MagicMock(), message="another poller",
            ))
            with self.assertRaises(TelegramConflictError):
                await middleware(request, self.telegram, SimpleNamespace(__api_method__="getUpdates"))
            await finished.wait()

        finished = asyncio.Event()
        self.dispatcher.start_polling.side_effect = poll
        self.dispatcher.stop_polling = AsyncMock(side_effect=stop)
        with self.assertRaisesRegex(IsolationError, "Another process"):
            await self.launch()
        self.dispatcher.stop_polling.assert_awaited_once()

    def test_worker_rejects_ephemeral_path(self):
        with self.assertRaisesRegex(IsolationError, "mounted"):
            worker.test_database_path(Path(self.folder.name))

    async def test_backup_and_restore_candidate_keep_live_database(self):
        backup = self.database.with_name("backup.sqlite")
        candidate = self.database.with_name("candidate.sqlite")
        with closing(sqlite3.connect(self.database)) as live:
            self.assertEqual(live.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
            live.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY)")
            live.execute("CREATE TABLE offers (id INTEGER PRIMARY KEY)")
            live.execute("INSERT INTO offers VALUES (1)")
            live.execute("INSERT INTO leads VALUES (1)")
            live.commit()
            digest = backup_database(self.database, backup)
            self.assertEqual(len(digest), 64)
            self.assertEqual(verify_small_snapshot(backup), (1, 1))
            live.execute("INSERT INTO leads VALUES (2)")
            live.commit()
            backup_database(backup, candidate)
            self.assertEqual(live.execute("SELECT id FROM leads").fetchall(), [(1,), (2,)])
        with closing(sqlite3.connect(candidate)) as restored:
            self.assertEqual(restored.execute("SELECT id FROM leads").fetchall(), [(1,)])
        with self.assertRaises(FileExistsError):
            backup_database(self.database, backup)
