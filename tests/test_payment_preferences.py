"""Offline payment matching integration tests."""
import os
import tempfile
import unittest
import payment_preferences as payment

class PaymentPreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "payment.sqlite")
        await payment.init_schema(self.db)
        await payment.save_provider(101, [payment.CASH], self.db)
        await payment.save_provider(102, [payment.VAT], self.db)
        await payment.save_provider(103, [payment.NO_VAT], self.db)
        await payment.save_provider(104, [payment.CASH, payment.VAT], self.db)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_cash(self):
        await payment.save_lead(1, payment.CASH, self.db)
        self.assertEqual(await payment.eligible_providers(1, [101,102,103,104], self.db), [101,104])

    async def test_vat(self):
        await payment.save_lead(2, payment.VAT, self.db)
        self.assertEqual(await payment.eligible_providers(2, [101,102,103,104], self.db), [102,104])

    async def test_no_vat(self):
        await payment.save_lead(3, payment.NO_VAT, self.db)
        self.assertEqual(await payment.eligible_providers(3, [101,102,103,104], self.db), [103])

    async def test_any_and_missing_provider_preferences(self):
        await payment.save_lead(4, payment.ANY, self.db)
        self.assertEqual(await payment.eligible_providers(4, [101,102,103,104,105], self.db), [101,102,103,104])

    async def test_missing_lead_preference_fails_closed(self):
        self.assertEqual(await payment.eligible_providers(99, [101,102], self.db), [])

    async def test_invalid_values_rejected(self):
        with self.assertRaises(ValueError):
            await payment.save_lead(5, "Криптовалюта", self.db)
        with self.assertRaises(ValueError):
            await payment.save_provider(106, [payment.ANY], self.db)

    async def test_preference_update(self):
        await payment.save_lead(6, payment.CASH, self.db)
        await payment.save_lead(6, payment.VAT, self.db)
        self.assertEqual(await payment.eligible_providers(6, [101,102], self.db), [102])
