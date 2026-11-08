import unittest
import payment_preferences as payment
import provider_registry as registry

class CombinedMatchingSmokeTests(unittest.IsolatedAsyncioTestCase):
    def test_payment_categories(self):
        self.assertIn(payment.CASH, payment.METHODS)
        self.assertIn(payment.VAT, payment.METHODS)
        self.assertIn(payment.NO_VAT, payment.METHODS)
        self.assertIn('каток', registry.CATEGORIES)
        self.assertIn('Москва', registry.REGIONS)

    async def test_combined_matching(self):
        import os
        import tempfile
        import provider_dispatch as dispatch
        with tempfile.TemporaryDirectory() as directory:
            db = os.path.join(directory, "test.sqlite")
            await registry.init_schema(db)
            await payment.init_schema(db)
            entries = [
                (101, "каток", "Москва", "СЗАО", [payment.CASH]),
                (102, "каток", "Москва", "СЗАО", [payment.VAT]),
                (103, "каток", "Московская область", "Химки", [payment.VAT]),
                (104, "мини-погрузчик", "Москва", "СЗАО", [payment.VAT]),
                (105, "каток", registry.BOTH_REGIONS, "весь регион", [payment.CASH, payment.VAT]),
            ]
            for pid, category, region, area, methods in entries:
                await registry.register_provider(pid, "Исполнитель", "+79991234567", True, db)
                await registry.add_equipment(pid, category, "Своя техника", region, area, db)
                await payment.save_provider(pid, methods, db)

            async def match(lead_id, method, category, region, area):
                await payment.save_lead(lead_id, method, db)
                candidates = await dispatch.recipients_for_lead(category=category, region=region, area=area, db_path=db)
                return await payment.eligible_providers(lead_id, candidates, db)

            self.assertEqual(await match(1, payment.CASH, "каток", "Москва", "СЗАО"), [101, 105])
            self.assertEqual(await match(2, payment.VAT, "каток", "Москва", "СЗАО"), [102, 105])
            self.assertEqual(await match(3, payment.VAT, "каток", "Московская область", "Химки"), [103, 105])
            self.assertEqual(await match(4, payment.ANY, "мини-погрузчик", "Москва", "СЗАО"), [104])
            await registry.set_blocked(102, True, admin_id=9, admin_ids={9}, db_path=db)
            self.assertEqual(await match(5, payment.VAT, "каток", "Москва", "СЗАО"), [105])
