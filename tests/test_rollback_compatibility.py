"""Exercise an exact older revision on a retained synthetic copy, never a Worker."""
import asyncio
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

os.environ.update(BOT_TOKEN="123456:TEST", ADMIN_IDS="1,2", LEADS_CHANNEL_ID="-100123")
import bot
import storage
import admin_access
import provider_registry
import user_access
import render_test_backup as backups
from aiogram.fsm.storage.base import StorageKey

ROLLBACK = "02f8676d7c2699d5e53e662d184bec23497b868a"
ROOT = Path(__file__).resolve().parents[1]


class RollbackCompatibilityTests(unittest.TestCase):
    def test_exact_previous_revision_preserves_rows_drafts_and_access(self):
        # Retain all evidence; no database or checkout is removed or overwritten.
        evidence = Path(tempfile.mkdtemp(prefix="msk-rollback-"))
        old = evidence / "rollback-code"
        old.mkdir()
        archive = subprocess.check_output(["git", "archive", ROLLBACK], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
            contents.extractall(old, filter="data")
        source = evidence / "synthetic.sqlite"

        async def seed():
            with patch.object(storage, "DB_PATH", str(source)):
                await bot.init_db()
                await provider_registry.register_provider(30,"Тест","+79991234567",True,str(source))
                await provider_registry.save_alert_preferences(30,["автокран"],["Москва"],0,str(source))
                await user_access.change(1,30,["full"],"Проверка отката","rollback-access",
                                         expected_revision=0,owner_id=1,legacy_admins={1,2})
                await admin_access.assign_role(1,2,"none","rollback-revoke",owner_id=1)
                drafts = bot.SQLiteDraftStorage(str(source))
                await drafts.init_schema()
                states = [bot.LeadForm.attachments,bot.OfferForm.price,bot.admin_ui.FindUser.results,
                          bot.admin_ui.FindLead.query,bot.admin_ui.ManageAdmins.confirm,
                          bot.admin_ui.ManageAccess.confirm]
                for index, state in enumerate(states):
                    await drafts.checkpoint(StorageKey(bot_id=99,chat_id=1,user_id=1,
                        destiny=f"rollback_{index}"),state,{"sentinel":index,"lead_id":11})
        asyncio.run(seed())
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO leads VALUES (11,20,'Тест','автокран','Москва','завтра','','+79991234567','published','2026-11-11')")
            db.execute("INSERT INTO offers VALUES (1,11,31,'Тест','test','32000','Без комментария','+79991234568','2026-11-11','sent')")
            db.execute("INSERT INTO providers VALUES (31,'Тест','автокран','Москва','+79991234568','2026-11-11')")
        snapshot, digest = backups.snapshot(source)
        candidate = backups.restore_candidate(snapshot, digest)
        probe = r'''
import asyncio, socket, sys
def deny(*args, **kwargs):
    raise AssertionError("Network is forbidden in rollback rehearsal")
socket.socket.connect = deny
import bot, storage, user_access, admin_access
from aiogram.fsm.storage.base import StorageKey
storage.DB_PATH = sys.argv[1]
async def check():
    await bot.init_db()
    drafts = bot.SQLiteDraftStorage(storage.DB_PATH)
    await drafts.init_schema()
    states = [bot.LeadForm.attachments,bot.OfferForm.price,bot.admin_ui.FindUser.results,
              bot.admin_ui.FindLead.query,bot.admin_ui.ManageAdmins.confirm,
              bot.admin_ui.ManageAccess.confirm]
    for index,state in enumerate(states):
        key=StorageKey(bot_id=99,chat_id=1,user_id=1,destiny=f"rollback_{index}")
        assert await drafts.get_state(key) == state.state
        assert await drafts.get_data(key) == {"sentinel":index,"lead_id":11}
    assert not await admin_access.allowed(2,"export",{1,2},1)
    assert (await user_access.snapshot(30))["scopes"] == ["full"]
    for scope in ("requests","offers","notifications"):
        async with __import__("aiosqlite").connect(storage.DB_PATH) as db:
            assert not await user_access.allowed_in(db,30,scope)
asyncio.run(check())
'''
        for code in (old, old, ROOT):
            subprocess.run([sys.executable,"-c",probe,str(candidate)],cwd=code,
                           check=True,timeout=30,capture_output=True)
            self.assertGreater(backups.verify_preserved_rows(snapshot,candidate),20)
        self.assertGreater(backups.verify_preserved_rows(snapshot,source),20)
        print(f"Rollback evidence retained: {evidence}; snapshot SHA-256: {digest}")
