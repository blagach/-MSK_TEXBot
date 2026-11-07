"""Offline lifecycle tests: no Telegram network, no production database."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import storage


class LeadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp.name, "isolated.sqlite")
        self.patcher = patch.object(storage, "DB_PATH", self.db_path)
        self.patcher.start()
        with sqlite3.connect(self.db_path) as db:
            db.execute("""CREATE TABLE leads (
                id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'new'
            )""")
            db.execute("INSERT INTO leads(id,status) VALUES (1,'new'),(2,'new'),(3,'new')")
        await storage.init_schema()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def status(self, lead_id):
        with sqlite3.connect(self.db_path) as db:
            return db.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[0]

    async def test_publish_and_offer_once(self):
        self.assertTrue(await storage.transition(1, "new", "publishing"))
        self.assertFalse(await storage.transition(1, "new", "publishing"))
        await storage.finish_publication(1, -10001, 42)
        self.assertEqual(self.status(1), "published")
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM lead_publications WHERE lead_id=1").fetchone()[0], 1)
        offer_id = await storage.save_offer(1, 101, "Исполнитель", "@test", "24000", "Смена", "+79990000000")
        self.assertIsNotNone(offer_id)
        self.assertIsNone(await storage.save_offer(1, 101, "Исполнитель", "@test", "24000", "Смена", "+79990000000"))
        await storage.mark_offer(offer_id, "sent")
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT delivery_status FROM offers WHERE id=?", (offer_id,)).fetchone()[0], "sent")

    async def test_rejection_prevents_publication_and_offer(self):
        self.assertTrue(await storage.transition(2, "new", "rejected"))
        self.assertFalse(await storage.transition(2, "new", "publishing"))
        self.assertIsNone(await storage.save_offer(2, 101, "Исполнитель", "@test", "24000", "Смена", "+79990000000"))
        self.assertEqual(self.status(2), "rejected")

    async def test_unpublished_lead_rejects_offer(self):
        self.assertIsNone(await storage.save_offer(3, 101, "Исполнитель", "@test", "24000", "Смена", "+79990000000"))

    async def test_unexpected_transition_is_rejected(self):
        with self.assertRaises(ValueError):
            await storage.transition(1, "new", "published")

    async def test_invalid_offer_delivery_state_rejected(self):
        with self.assertRaises(ValueError):
            await storage.mark_offer(1, "invalid")

    async def test_duplicate_publication_rejected(self):
        self.assertTrue(await storage.transition(1, "new", "publishing"))
        await storage.finish_publication(1, -10001, 42)
        with self.assertRaises(ValueError):
            await storage.finish_publication(1, -10001, 43)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM lead_publications WHERE lead_id=1").fetchone()[0], 1)
