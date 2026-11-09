"""Launch the isolated Telegram worker with a fail-closed test configuration."""
import asyncio
import importlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from aiogram import Bot
from aiogram.exceptions import TelegramConflictError
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands
from draft_storage import SQLiteDraftStorage

from render_test_backup import snapshot, verify_small_snapshot
from render_test_guard import IsolationError, TelegramIsolation, verify_identity

TEST_BOT = "Msktex_test_dmitry_bot"
TEST_WORKER_ID = "srv-dboqc51srm7s73cppi00"
DISK = Path("/var/data")
POLLING_UPDATES = ["message", "callback_query"]


@dataclass(frozen=True)
class TestConfiguration:
    token: str
    admin_id: int
    channel_id: int
    testers: frozenset[int]
    support_group_id: int | None = None


def read_test_configuration(env: Mapping[str, str]) -> TestConfiguration:
    if any(env.get(name) for name in (
        "BOT_TOKEN", "BOT_USERNAME", "ADMIN_IDS", "LEADS_CHANNEL_ID", "PAYMENT_URL",
        "TEST_NOTIFICATION_PROVIDER_IDS", "DATABASE_URL", "SUPPORT_GROUP_ID",
    )):
        raise IsolationError("Shared bot settings must be absent from the test worker")
    token = env.get("TEST_BOT_TOKEN", "").strip()
    if not token:
        raise IsolationError("Missing TEST_BOT_TOKEN")
    try:
        admin_id = int(env.get("TEST_ADMIN_ID", ""))
        channel_id = int(env.get("TEST_CHANNEL_ID", ""))
        group = env.get("TEST_SUPPORT_GROUP_ID", "").strip()
        support_group_id = int(group) if group else None
        testers = frozenset(int(value.strip()) for value in env.get("TESTER_IDS", "").split(","))
    except ValueError:
        raise IsolationError("Invalid test account or channel IDs") from None
    if support_group_id is not None and (support_group_id >= 0 or support_group_id == channel_id):
        raise IsolationError("The support group must be separate from the test channel")
    return TestConfiguration(token, admin_id, channel_id, testers, support_group_id)


def test_database_path(disk: Path = DISK) -> Path:
    if disk.is_symlink() or not disk.is_mount() or not os.access(disk, os.W_OK):
        raise IsolationError("The writable test disk must be mounted at /var/data")
    database = disk / "bot.db"
    if database.is_symlink() or (database.exists() and not database.is_file()):
        raise IsolationError("Invalid test SQLite path")
    return database


async def start() -> None:
    if os.environ.get("RENDER_SERVICE_ID") != TEST_WORKER_ID:
        raise IsolationError("This launcher is restricted to the MSK TEXBot test Worker")
    config = read_test_configuration(os.environ)
    guard = TelegramIsolation(config.testers, config.admin_id, config.channel_id, config.support_group_id)
    database = test_database_path()
    polling_conflict = asyncio.Event()

    async def prevent_duplicate_polling(make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramConflictError:
            if method.__api_method__ == "getUpdates":
                polling_conflict.set()
            raise

    async with Bot(token=config.token) as telegram:
        verify_identity(await telegram.get_me(), TEST_BOT)
        if (await telegram.get_webhook_info()).url:
            raise IsolationError("The test bot has an active webhook")
        telegram.session.middleware(guard.outgoing)
        telegram.session.middleware(prevent_duplicate_polling)
        await guard.check_channel(telegram)

        if database.exists():
            backup_path, digest = snapshot(database)
            logging.info("Test SQLite backup %s SHA-256 %s", backup_path.name, digest)
            counts = verify_small_snapshot(backup_path)
            if counts is not None:
                logging.info("Test SQLite restore verified: leads=%s offers=%s", *counts)
            else:
                logging.info("Test SQLite restore requires a separate file (snapshot exceeds 16 MiB)")

        os.environ.update(
            BOT_TOKEN=config.token, BOT_USERNAME=TEST_BOT, ADMIN_IDS=str(config.admin_id),
            LEADS_CHANNEL_ID=str(config.channel_id), PAYMENT_URL="",
            TEST_NOTIFICATION_PROVIDER_IDS=",".join(map(str, sorted(config.testers))),
        )
        if config.support_group_id is not None:
            os.environ["SUPPORT_GROUP_ID"] = str(config.support_group_id)
        app = importlib.import_module("bot")
        app.storage.DB_PATH = str(database)
        await app.init_db()
        drafts = SQLiteDraftStorage(str(database))
        await drafts.init_schema()
        dispatcher = app.create_dispatcher(drafts)
        dispatcher.update.outer_middleware(guard.incoming)
        await telegram.set_my_commands([
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="menu", description="Восстановить кнопки и продолжить анкету"),
            BotCommand(command="cancel", description="Отменить текущую анкету"),
            BotCommand(command="support", description="Помощь и поддержка"),
        ], scope=BotCommandScopeAllPrivateChats())
        await telegram.set_chat_menu_button(menu_button=MenuButtonCommands())
        logging.info("Starting isolated @%s at %s", TEST_BOT, os.getenv("RENDER_GIT_COMMIT", "local"))
        polling = asyncio.create_task(dispatcher.start_polling(
            telegram, allowed_updates=POLLING_UPDATES, close_bot_session=False,
        ))
        conflict = asyncio.create_task(polling_conflict.wait())
        try:
            await asyncio.wait({polling, conflict}, return_when=asyncio.FIRST_COMPLETED)
            if polling_conflict.is_set():
                raise IsolationError("Another process is polling the test bot; stop it before restarting")
            await polling
        finally:
            if not polling.done():
                await dispatcher.stop_polling()
            conflict.cancel()
            await asyncio.gather(polling, conflict, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(start())
    except IsolationError as error:
        raise SystemExit(str(error)) from None
    except TelegramConflictError:
        raise SystemExit("Another process is polling the test bot") from None
    except Exception as error:
        raise SystemExit(f"Test startup failed: {type(error).__name__}") from None
