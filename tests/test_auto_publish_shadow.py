import unittest
from auto_publish_shadow import decide

BASE = dict(category="каток",region="Москва",area="СЗАО",
            place="улица Свободы",work_date="завтра 9:00",
            details="Уплотнить площадку",recent_requests=0,duplicate=False)

class ShadowModeTests(unittest.TestCase):
    def test_disabled_even_for_approved_customer(self):
        self.assertEqual(decide(approved=True,**BASE).route,"manual")
    def test_approved_clean_is_only_candidate(self):
        self.assertEqual(decide(enabled=True,approved=True,**BASE).route,"candidate")
    def test_first_customer_stays_manual(self):
        self.assertEqual(decide(enabled=True,approved=False,**BASE).route,"manual")
    def test_duplicate_stays_manual(self):
        self.assertEqual(decide(enabled=True,approved=True,**{**BASE,"duplicate":True}).route,"manual")
    def test_rate_limit_stays_manual(self):
        self.assertEqual(decide(enabled=True,approved=True,**{**BASE,"recent_requests":3}).route,"manual")
    def test_suspicious_link_stays_manual(self):
        self.assertEqual(decide(enabled=True,approved=True,**{**BASE,"details":"https://example.com"}).route,"manual")
