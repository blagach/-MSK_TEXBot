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

    async def test_stale_review_cannot_restore_revoked_rights_and_audit_has_old_role(self):
        snapshot = await access.role_snapshot(2, {1, 2}, 1)
        self.assertEqual(snapshot, {"role": "administrator", "revision": 0})
        await access.assign_role(1, 2, "none", "revoke", owner_id=1, expected_revision=0, legacy_admins={1, 2})
        with self.assertRaises(access.RoleConflict):
            await access.assign_role(1, 2, "administrator", "old-review", owner_id=1,
                                     expected_revision=snapshot["revision"], legacy_admins={1, 2})
        self.assertFalse(await access.allowed(2, "export", {1, 2}, 1))
        history = await access.history()
        self.assertEqual(history[0]["result"], "conflict")
        self.assertIn('"previous_role": "administrator"', history[1]["details"])
        self.assertEqual((await access.role_snapshot(2, {1, 2}, 1))["role"], "none")

    async def test_two_distinct_confirmations_of_same_revision_only_change_once(self):
        results = await asyncio.gather(*(
            access.assign_role(1, 3, role, request, owner_id=1, expected_revision=0)
            for role, request in (("support", "first"), ("moderator", "second"))
        ), return_exceptions=True)
        self.assertEqual(sum(item is True for item in results), 1)
        self.assertEqual(sum(isinstance(item, access.RoleConflict) for item in results), 1)

    async def test_administrator_pages_keep_owner_legacy_and_revoked_assignments(self):
        await access.assign_role(1, 2, "none", "revoke", owner_id=1)
        await access.assign_role(1, 3, "support", "assign", owner_id=1)
        page = await access.administrator_page({1, 2}, 1, limit=2)
        self.assertEqual([(r["id"], r["role"]) for r in page], [(1, "owner"), (2, "none")])
        second = await access.administrator_page({1, 2}, 1, after=2)
        self.assertEqual([(r["id"], r["role"]) for r in second], [(3, "support")])
        self.assertEqual(len(await access.administrator_page({1, 2}, 1)), 3)

    async def test_one_export_callback_is_claimed_once_under_concurrency(self):
        before=None
        with sqlite3.connect(self.path) as db:
            before=db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        result=await asyncio.gather(access.reserve_export(1,'same-callback'),access.reserve_export(1,'same-callback'))
        self.assertEqual(sorted(result),[False,True])
        self.assertTrue(await access.reserve_export(2,'same-callback'))
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),before)
            self.assertEqual(db.execute('SELECT * FROM leads').fetchall(),[(11,'published')])

    async def test_audit_filters_keep_pagination_and_do_not_change_history(self):
        for action,result in (('admin_view','success'),('role_changed','success'),('user_access','conflict'),
                              ('export_requested','accepted'),('export_delivery','uncertain'),('export_sent','success')):
            await access.record(1,action,result=result)
        all_rows=await access.history(limit=100)
        self.assertEqual(len(await access.history(kind='permissions')),2)
        self.assertEqual(len(await access.history(kind='errors')),2)
        exports=await access.history(kind='exports')
        self.assertEqual(len(exports),3)
        self.assertEqual(len(await access.history(exports[0]['id'],kind='exports')),2)
        with self.assertRaises(ValueError):
            await access.history(kind="exports OR 1=1")
        self.assertEqual([dict(r) for r in await access.history(limit=100)],[dict(r) for r in all_rows])
