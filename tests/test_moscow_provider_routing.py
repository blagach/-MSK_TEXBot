"""Moscow and Oblast provider routing, offline and isolated."""
import os
import tempfile
import unittest

import provider_registry as registry
from provider_dispatch import recipients_for_lead, notification_text


class MoscowProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temp.name, "providers.sqlite")
        await registry.init_schema(self.db)
        for user in (101, 102, 103, 104, 105):
            await registry.register_provider(user, "Исполнитель", "+79991234567", True, self.db)
        await registry.add_equipment(101, "каток", "Москва СЗАО", "Москва", "СЗАО", self.db)
        await registry.add_equipment(102, "каток", "МО Химки", "Московская область", "Химки", self.db)
        await registry.add_equipment(103, "каток", "Оба региона", registry.BOTH_REGIONS, "весь регион", self.db)
        await registry.add_equipment(104, "мини-погрузчик", "Другая категория", "Москва", "СЗАО", self.db)
        await registry.add_equipment(105, "каток", "Москва целиком", "Москва", "весь регион", self.db)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def match(self, category, region, area):
        return await recipients_for_lead(category=category, region=region, area=area, db_path=self.db)

    async def test_moscow_and_oblast_do_not_leak_between_regions(self):
        self.assertEqual(await self.match("каток", "Москва", "СЗАО"), [101, 103, 105])
        self.assertEqual(await self.match("каток", "Московская область", "Химки"), [102, 103])
        self.assertEqual(await self.match("каток", "Московская область", "Одинцово"), [103])

    async def test_wrong_category_and_invalid_location_not_broadcast(self):
        self.assertEqual(await self.match("мини-погрузчик", "Москва", "СЗАО"), [104])
        self.assertEqual(await self.match("каток", "Москва", ""), [])
        self.assertEqual(await self.match("каток", "Россия", "СЗАО"), [])
        self.assertEqual(await self.match("неизвестная", "Москва", "СЗАО"), [])

    async def test_opt_out_block_and_inactive_are_excluded(self):
        await registry.set_notifications(101, False, self.db)
        await registry.set_blocked(103, True, admin_id=9, admin_ids={9}, db_path=self.db)
        equipment = await registry.list_equipment(105, self.db)
        await registry.update_equipment(105, equipment[0]["id"], category="каток",
            description="Москва целиком", region="Москва", area="весь регион",
            active=False, db_path=self.db)
        self.assertEqual(await self.match("каток", "Москва", "СЗАО"), [])

    async def test_multiple_equipment_same_provider_not_duplicated(self):
        await registry.add_equipment(101, "каток", "Второй каток", "Москва", "СЗАО", self.db)
        self.assertEqual(await self.match("каток", "Москва", "СЗАО"), [101, 103, 105])

    async def test_notification_excludes_customer_phone(self):
        message = notification_text(lead_id=77, category="каток", region="Москва",
            area="СЗАО", work_date="завтра", details="Укатать площадку",
            bot_username="@MSK_TEXBot")
        self.assertIn("lead_77", message)
        self.assertNotIn("+79991234567", message)
