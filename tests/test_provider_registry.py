"""Isolated tests for provider registration and matching; no live Telegram calls."""
import asyncio
import os
import tempfile
import unittest

import provider_registry as registry


class ProviderRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temp.name, "test.sqlite3")
        await registry.init_schema(self.db)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_registration_requires_consent(self):
        with self.assertRaises(ValueError):
            await registry.register_provider(100, "Компания", "+79991234567", False, self.db)

    async def test_registration_is_idempotent_and_preserves_block(self):
        await registry.register_provider(100, "Компания", "+79991234567", True, self.db)
        self.assertTrue(await registry.set_blocked(100, True, admin_id=9, admin_ids={9}, db_path=self.db))
        await registry.register_provider(100, "Новое имя", "+79991234567", True, self.db)
        with self.assertRaises(ValueError):
            await registry.add_equipment(100, "каток", "4 тонны", "Москва", "весь регион", self.db)

    async def test_multiple_equipment_and_targeted_matching(self):
        await registry.register_provider(100, "Компания", "+79991234567", True, self.db)
        await registry.add_equipment(100, "каток", "4 тонны", "Москва", "весь регион", self.db)
        await registry.add_equipment(100, "мини-погрузчик", "Bobcat", "Москва", "СЗАО", self.db)
        self.assertEqual(len(await registry.list_equipment(100, self.db)), 2)
        self.assertEqual(await registry.matching_providers("каток", "Москва", "ЦАО", self.db), [100])
        self.assertEqual(await registry.matching_providers("мини-погрузчик", "Москва", "СЗАО", self.db), [100])
        self.assertEqual(await registry.matching_providers("мини-погрузчик", "Москва", "ЦАО", self.db), [])

    async def test_notifications_and_blocking(self):
        await registry.register_provider(100, "Компания", "+79991234567", True, self.db)
        await registry.add_equipment(100, "каток", "4 тонны", "Москва", "весь регион", self.db)
        await registry.set_notifications(100, False, self.db)
        self.assertEqual(await registry.matching_providers("каток", "Москва", "ЦАО", self.db), [])
        await registry.set_notifications(100, True, self.db)
        with self.assertRaises(PermissionError):
            await registry.set_blocked(100, True, admin_id=10, admin_ids={9}, db_path=self.db)
        await registry.set_blocked(100, True, admin_id=9, admin_ids={9}, db_path=self.db)
        self.assertEqual(await registry.matching_providers("каток", "Москва", "ЦАО", self.db), [])

    async def test_cannot_edit_someone_elses_equipment(self):
        await registry.register_provider(100, "Компания", "+79991234567", True, self.db)
        await registry.register_provider(200, "Другой", "+79991234568", True, self.db)
        item = await registry.add_equipment(100, "каток", "4 тонны", "Москва", "весь регион", self.db)
        result = await registry.update_equipment(
            200, item, category="каток", description="Чужая техника",
            region="Москва", area="весь регион", db_path=self.db)
        self.assertFalse(result)
        self.assertEqual((await registry.list_equipment(100, self.db))[0]["description"], "4 тонны")


if __name__ == "__main__":
    unittest.main()

    async def test_owner_edits_all_equipment_fields_and_pauses_without_deleting(self):
        await registry.register_provider(100,'Исполнитель','+79991234567',True,self.db)
        item=await registry.add_equipment(100,'каток','Старая карточка','Москва','ЦАО',self.db)
        self.assertTrue(await registry.update_equipment(100,item,category='мини-погрузчик',description=' Новая карточка ',region='Московская область',area=' Химки ',active=False,db_path=self.db))
        rows=await registry.list_equipment(100,self.db)
        self.assertEqual(len(rows),1)
        row=dict(rows[0])
        self.assertEqual(row['id'],item)
        for key,value in {'category':'мини-погрузчик','description':'Новая карточка','region':'Московская область','area':'Химки','active':0}.items():
            with self.subTest(field=key): self.assertEqual(row[key],value)
        self.assertEqual(await registry.matching_providers('мини-погрузчик','Московская область','Химки',self.db),[])
        self.assertTrue(await registry.update_equipment(100,item,category=row['category'],description=row['description'],region=row['region'],area=row['area'],active=True,db_path=self.db))
        self.assertEqual(await registry.matching_providers('мини-погрузчик','Московская область','Химки',self.db),[100])
        self.assertEqual((await registry.list_equipment(100,self.db))[0]['id'],item)
