import unittest
import payment_preferences as payment
import provider_registry as registry

class CombinedMatchingSmokeTests(unittest.TestCase):
    def test_payment_categories(self):
        self.assertIn(payment.CASH, payment.METHODS)
        self.assertIn(payment.VAT, payment.METHODS)
        self.assertIn(payment.NO_VAT, payment.METHODS)
        self.assertIn('каток', registry.CATEGORIES)
        self.assertIn('Москва', registry.REGIONS)
