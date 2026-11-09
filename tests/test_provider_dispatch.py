import os
import tempfile
import unittest

import provider_registry as registry
from provider_dispatch import notification_text, recipients_for_lead, service_direction
import waste_requests


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
        self.assertEqual(set(result), {101, 103})

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

    async def test_paused_machine_does_not_match_equipment_request(self):
        item = (await registry.list_equipment(101, self.db))[0]
        await registry.change_equipment_status(101, item["id"], False, self.db)
        result = await recipients_for_lead(category="мини-погрузчик", region="Московская область", area="Химки", db_path=self.db)
        self.assertEqual(result, [103])

    def test_services_use_structured_values_and_classification_gate(self):
        for direction in registry.SERVICE_DIRECTIONS:
            self.assertEqual(service_direction(direction), direction)
        self.assertEqual(service_direction(waste_requests.CATEGORY, "snow"), "Вывоз снега")
        self.assertEqual(service_direction(waste_requests.CATEGORY, "construction", "V"),
                         "Вывоз строительных отходов")
        for hazard in (None, "Не знаю", "I", "II", "III", "IV"):
            self.assertIsNone(service_direction(waste_requests.CATEGORY, "construction", hazard))
        self.assertIsNone(service_direction("самосвал"))

    def test_notification_contains_no_phone(self):
        text = notification_text(lead_id=17, category="мини-погрузчик", region="Московская область", area="Химки", work_date="завтра", details="Погрузка грунта", bot_username="MSK_TEXBot")
        self.assertIn("lead_17", text)
        self.assertIn("Химки", text)
        self.assertNotIn("+79991234567", text)


if __name__ == "__main__":
    unittest.main()
