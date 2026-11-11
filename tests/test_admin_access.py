"""Owner authority, role revisions and append-only audit are checked on SQLite."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import admin_access as access
import storage


class AdminAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "access.sqlite"
        patcher = patch.object(storage, "DB_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE leads(id INTEGER PRIMARY KEY, status TEXT)")
            db.execute("INSERT INTO leads VALUES (11,'published')")
        await access.init_schema()

    async def test_owner_is_explicit_and_ordinary_admin_cannot_grant_roles(self):
        self.assertTrue(await access.allowed(2, "moderate", {1, 2}))
        self.assertFalse(await access.allowed(2, "roles", {1, 2}))
        self.assertFalse(await access.allowed(1, "roles", {1, 2}))
        self.assertTrue(await access.allowed(1, "roles", {1, 2}, 1))
        for actor, owner in ((2, 1), (1, None)):
            with self.assertRaises(PermissionError):
                await access.assign_role(actor, 3, "support", "denied", owner_id=owner)
        with self.assertRaises(ValueError):
            await access.assign_role(1, 1, "none", "self", owner_id=1)
        with self.assertRaises(ValueError):
            await access.assign_role(1, 3, "owner", "other-owner", owner_id=1)

    async def test_role_changes_and_revocation_override_legacy_access_and_recipients(self):
        await access.assign_role(1, 2, "support", "support", owner_id=1)
        self.assertTrue(await access.allowed(2, "support", {1, 2}, 1))
        self.assertFalse(await access.allowed(2, "moderate", {1, 2}, 1))
        self.assertFalse(await access.allowed(2, "export", {1, 2}, 1))
        self.assertEqual(await access.staff("moderate", {1, 2}, 1), [1])
        await access.assign_role(1, 2, "none", "revoke", owner_id=1)
        self.assertFalse(await access.allowed(2, "panel", {1, 2}, 1))
        self.assertEqual(await access.staff("support", {1, 2}, 1), [1])
        await access.init_schema()
        self.assertFalse(await access.allowed(2, "panel", {1, 2}, 1))

    async def test_duplicate_confirmation_is_idempotent_and_history_survives(self):
        results = await asyncio.gather(*(
            access.assign_role(1, 3, "moderator", "one-request", owner_id=1) for _ in range(2)
        ))
        self.assertEqual(sorted(results), [False, True])
        with self.assertRaises(ValueError):
            await access.assign_role(1, 4, "support", "one-request", owner_id=1)
        await access.assign_role(1, 3, "none", "revoke", owner_id=1)
        history = await access.history()
        self.assertEqual(len(history), 2)
        self.assertTrue(all(row["actor_id"] == 1 and row["created_at"] for row in history))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM admin_role_events").fetchone(), (2,))
            self.assertEqual(db.execute("SELECT * FROM leads").fetchall(), [(11, "published")])

    async def test_restore_and_repeat_schema_preserve_every_original_row(self):
        import render_test_backup as backups
        snapshot, digest = backups.snapshot(self.path)
        restored = backups.restore_candidate(snapshot, digest)
        with patch.object(storage, "DB_PATH", str(restored)):
            for _ in range(2):
                await access.init_schema()
                self.assertEqual(backups.verify_preserved_rows(snapshot, restored), 4)
            self.assertFalse(await access.allowed(2, "roles", {1, 2}))
        self.assertEqual(backups.verify_preserved_rows(snapshot, self.path), 4)
