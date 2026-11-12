import unittest
from datetime import datetime, timezone
from work_datetime import parse_work_datetime, MOSCOW

class WorkDatetimeTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,12,31,23,30,tzinfo=MOSCOW)
    def test_relative_year_rollover(self):
        self.assertEqual(parse_work_datetime('завтра 09:00',now=self.now),'01.01.2027 09:00')
    def test_future_explicit(self):
        self.assertEqual(parse_work_datetime('01.01.2027 10:00',now=self.now),'01.01.2027 10:00')
    def test_invalid_and_past_rejected(self):
        for value in ['31.12.2026 23:30','31.12.2026 22:00','31.02.2027 09:00','завтра 25:00','когда-нибудь','01.01.2027']:
            with self.subTest(value=value),self.assertRaises(ValueError):
                parse_work_datetime(value,now=self.now)
    def test_clock_converted_to_moscow(self):
        now=datetime(2026,12,31,22,0,tzinfo=timezone.utc)
        self.assertEqual(parse_work_datetime('сегодня 02:00',now=now),'01.01.2027 02:00')
    def test_dot_separator_and_case_keep_moscow_date(self):
        for value in ('Завтра 09.00','завтра 09:00','01.01.2027 09.00','01.01.2027 09:00'):
            self.assertEqual(parse_work_datetime(value,now=self.now),'01.01.2027 09:00')
    def test_ambiguous_time_is_not_invented_and_reason_is_specific(self):
        with self.assertRaisesRegex(ValueError,'точное время'):
            parse_work_datetime('01.01.2027 после 9 утра',now=self.now)
        with self.assertRaisesRegex(ValueError,'уже прошли'):
            parse_work_datetime('31.12.2026 09.00',now=self.now)
