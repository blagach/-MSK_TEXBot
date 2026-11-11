"""Explicit alert subscriptions preserve legacy cards and availability."""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

import provider_registry as registry


class PreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / "preferences.sqlite")
        await registry.init_schema(self.path)
        await registry.register_provider(101, "Исполнитель", "+79991234567", True, self.path)

    async def match(self, category="автокран", region="Москва", area="ЦАО"):
        return await registry.matching_providers(category, region, area, self.path)

    async def test_explicit_types_and_regions_work_without_inventory_and_do_not_duplicate(self):
        self.assertEqual(await self.match(), [])
        self.assertTrue(await registry.save_alert_preferences(
            101, ["автокран", "самосвал", "другая техника", "автокран"], ["Москва", "Московская область"], 0, self.path,
        ))
        for category in ("автокран", "самосвал", "другая техника"):
            self.assertEqual(await self.match(category), [101])
            self.assertEqual(await self.match(category, "Московская область", "Химки"), [101])
        self.assertEqual(await self.match("экскаватор-погрузчик"), [])
        await registry.set_notifications(101, False, self.path)
        self.assertEqual(await self.match(), [])
        await registry.set_notifications(101, True, self.path)
        await registry.set_blocked(101, True, admin_id=1, admin_ids={1}, db_path=self.path)
        self.assertEqual(await self.match(), [])

    async def test_legacy_pause_blocks_only_until_independent_subscription_is_saved(self):
        machine = await registry.add_equipment(101, "автокран", "Кран", "Москва", "СЗАО", self.path)
        self.assertEqual(await self.match(area="СЗАО"), [101])
        self.assertEqual(await self.match(), [])
        await registry.update_equipment(101, machine, category="автокран", description="Кран",
                                        region="Москва", area="СЗАО", active=False, db_path=self.path)
        self.assertEqual(await self.match(area="СЗАО"), [])
        pref = await registry.alert_preferences(101, self.path)
        self.assertEqual(pref, {"revision": 0, "categories": ["автокран"], "regions": ["Москва"]})
        with sqlite3.connect(self.path) as db:
            before = db.execute("SELECT * FROM provider_equipment").fetchall()
        await registry.save_alert_preferences(101, ["автокран", "самосвал"], ["Московская область"], 0, self.path)
        self.assertEqual(await self.match(), [])
        self.assertEqual(await self.match(region="Московская область", area="Химки"), [101])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT * FROM provider_equipment").fetchall(), before)
        await registry.update_equipment(101, machine, category="автокран", description="Кран",
                                        region="Москва", area="СЗАО", active=False, db_path=self.path)
        self.assertEqual(await self.match(region="Московская область"), [101])
        self.assertEqual(await self.match("самосвал", "Московская область"), [101])
        await registry.update_equipment(101, machine, category="автокран", description="Кран",
                                        region="Москва", area="СЗАО", active=True, db_path=self.path)
        self.assertEqual(await self.match(region="Московская область"), [101])

    async def test_revisions_survive_restart_and_stale_or_concurrent_save_cannot_overwrite(self):
        results = await asyncio.gather(*(
            registry.save_alert_preferences(101, [category], ["Москва"], 0, self.path)
            for category in ("самосвал", "автокран")
        ))
        self.assertEqual(sorted(results), [False, True])
        before = await registry.alert_preferences(101, self.path)
        await registry.init_schema(self.path)
        self.assertEqual(await registry.alert_preferences(101, self.path), before)
        self.assertFalse(await registry.save_alert_preferences(101, ["КДМ"], ["Москва"], 0, self.path))
        self.assertTrue(await registry.save_alert_preferences(101, ["каток"], ["Московская область"], 1, self.path))
        self.assertEqual(await self.match("дорожный каток", "Московская область"), [101])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM provider_alert_preferences").fetchone(), (2,))

    async def test_invalid_preferences_and_unregistered_user_do_not_write(self):
        for categories, regions in (([], ["Москва"]), (["неизвестно"], ["Москва"]), (["КДМ"], []),
                                    (["КДМ"], ["неизвестно"])):
            with self.assertRaises(ValueError):
                await registry.save_alert_preferences(101, categories, regions, 0, self.path)
        with self.assertRaises(ValueError):
            await registry.save_alert_preferences(999, ["КДМ"], ["Москва"], 0, self.path)
        self.assertEqual((await registry.alert_preferences(101, self.path))["revision"], 0)
