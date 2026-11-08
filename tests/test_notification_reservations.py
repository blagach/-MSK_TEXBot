"""Tests for offline notification reservation safety; no Telegram API calls."""
import os
import sqlite3
import tempfile
import unittest

import notification_reservations as ledger
import provider_registry


class NotificationReservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.sqlite")
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE leads(id INTEGER PRIMARY KEY, status TEXT NOT NULL)")
            con.executemany("INSERT INTO leads VALUES (?,?)", [(1, "new"), (2, "published"), (3, "published")])
        await provider_registry.init_schema(self.db)
        await provider_registry.register_provider(101, "Тест", "+79990000000", True, self.db)
        await provider_registry.register_provider(102, "Тест 2", "+79990000001", True, self.db)
        await ledger.init_schema(self.db)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_unpublished_never_reserved(self):
        self.assertFalse(await ledger.reserve(1, 101, self.db))

    async def test_reservation_survives_retries(self):
        self.assertTrue(await ledger.reserve(2, 101, self.db))
        self.assertFalse(await ledger.reserve(2, 101, self.db))
        self.assertTrue(await ledger.record_outcome(2, 101, "uncertain", self.db))
        self.assertFalse(await ledger.reserve(2, 101, self.db))
        self.assertFalse(await ledger.record_outcome(2, 101, "confirmed", self.db))

    async def test_blocked_and_opted_out_are_excluded(self):
        await provider_registry.set_notifications(101, False, self.db)
        self.assertFalse(await ledger.reserve(2, 101, self.db))
        await provider_registry.set_blocked(102, True, admin_id=9, admin_ids={9}, db_path=self.db)
        self.assertFalse(await ledger.reserve(2, 102, self.db))

    async def test_distinct_lead_and_provider_pairs(self):
        self.assertTrue(await ledger.reserve(2, 101, self.db))
        self.assertTrue(await ledger.reserve(3, 101, self.db))
        self.assertTrue(await ledger.reserve(2, 102, self.db))
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM provider_notification_reservations").fetchone()[0], 3)

    async def test_unknown_provider_and_invalid_status(self):
        self.assertFalse(await ledger.reserve(2, 999, self.db))
        with self.assertRaises(ValueError):
            await ledger.record_outcome(2, 101, "retry", self.db)
