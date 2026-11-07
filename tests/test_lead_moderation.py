import unittest
from lead_moderation import assess

BASE = dict(category="каток", region="Москва", area="СЗАО",
            place="улица Лесная", work_date="завтра 9:00",
            details="Уплотнить площадку")

class ModerationTests(unittest.TestCase):
    def test_new_customer_requires_review(self):
        self.assertEqual(assess(previously_approved=False, **BASE).status,"review")
    def test_approved_customer_is_only_eligible(self):
        self.assertEqual(assess(previously_approved=True, **BASE).status,"eligible")
    def test_duplicate_requires_review(self):
        self.assertEqual(assess(previously_approved=True, duplicate=True, **BASE).status,"review")
    def test_rate_limit_requires_review(self):
        self.assertEqual(assess(previously_approved=True, recent_requests=3, **BASE).status,"review")
    def test_link_requires_review(self):
        self.assertEqual(assess(previously_approved=True, **{**BASE,"details":"Смотрите https://example.com"}).status,"review")
    def test_missing_location_requires_review(self):
        self.assertEqual(assess(previously_approved=True, **{**BASE,"area":""}).status,"review")
