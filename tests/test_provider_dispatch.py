import os
import tempfile
import unittest

import provider_registry as registry
from provider_dispatch import notification_text, recipients_for_lead


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temp.name, "providers.sqlite3")
        await registry.init_schema(self.db)
        await registry.register_provider(101, "Химки", "+79991234567", True, db_path=self.db)
        await registry.register_provider(102, "Другой район", "+79991234568", True, db_path=self.db)
        await registry.register_provider(103, "Вся область", "+79991234569", True, db_path=self.db)
        await registry.add_equipment(101, "мини-погрузчик", "Bobcat", "Московская область", "Химки", db_path=self.db)
        await registry.add_equipment(102, "мини-погрузчик", "Bobcat", "Московская область", "Балашиха", db_path=self.db)
        await registry.add_equipment(103, "мини-погрузчик", "Bobcat", "Московская область", "весь регион", db_path=self.db)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_only_matching_area_and_region(self):
        result = await recipients_for_lead(category="мини-погрузчик", region="Московская область", area="Химки", db_path=self.db)
        self.assertEqual({row["telegram_id"] for row in result}, {101, 103})

    async def test_unknown_location_and_category_fail_closed(self):
        for category, region, area in [
            ("неизвестная", "Московская область", "Химки"),
            ("мини-погрузчик", "неизвестный регион", "Химки"),
            ("мини-погрузчик", "Московская область", ""),
        ]:
            self.assertEqual(await recipients_for_lead(category=category, region=region, area=area, db_path=self.db), [])

    async def test_opt_out_and_admin_block(self):
        await registry.set_notifications(101, False, db_path=self.db)
        await registry.set_blocked(103, True, admin_id=999, admin_ids={999}, db_path=self.db)
        result = await recipients_for_lead(category="мини-погрузчик", region="Московская область", area="Химки", db_path=self.db)
        self.assertEqual(result, [])

    def test_notification_contains_no_phone(self):
        text = notification_text(lead_id=17, category="мини-погрузчик", region="Московская область", area="Химки", work_date="завтра", details="Погрузка грунта", bot_username="MSK_TEXBot")
        self.assertIn("lead_17", text)
        self.assertIn("Химки", text)
        self.assertNotIn("+79991234567", text)


if __name__ == "__main__":
    unittest.main()
