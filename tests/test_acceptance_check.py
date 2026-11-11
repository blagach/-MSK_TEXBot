import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import acceptance_check as check


class AcceptanceEvidenceTests(unittest.TestCase):
    def fixture(self):
        directory = Path(tempfile.mkdtemp(prefix='msk-acceptance-evidence-'))
        path = directory / 'fixture.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('''
                CREATE TABLE leads(id INTEGER,customer_id INTEGER,status TEXT);
                CREATE TABLE lead_lifecycle_events(lead_id INTEGER,actor_id INTEGER,action TEXT,reason TEXT);
                CREATE TABLE support_tickets(id INTEGER,requester_id INTEGER);
                CREATE TABLE support_messages(id INTEGER,ticket_id INTEGER,actor_id INTEGER,is_admin INTEGER,text TEXT);
                CREATE TABLE support_reply_deliveries(message_id INTEGER,status TEXT,telegram_message_id INTEGER,error_code TEXT);
                CREATE TABLE admin_action_log(actor_id INTEGER,action TEXT,target_id INTEGER,result TEXT,details TEXT);
                INSERT INTO leads VALUES(17,20,'closed');
                INSERT INTO support_tickets VALUES(4,20);
                INSERT INTO support_reply_deliveries VALUES(9,'sent',111,NULL);
            ''')
            db.execute("INSERT INTO lead_lifecycle_events VALUES(17,1,'closed',?)", (check.CLOSE_REASON,))
            db.execute('INSERT INTO support_messages VALUES(8,4,20,0,?)', (check.QUESTION,))
            db.execute('INSERT INTO support_messages VALUES(9,4,1,1,?)', (check.ANSWER,))
            db.execute("INSERT INTO admin_action_log VALUES(1,'lead_close',17,'success','{}')")
            db.execute("INSERT INTO admin_action_log VALUES(1,'support_reply',4,'saved',?)",
                       (json.dumps({'message_id': 9}),))
            db.execute("INSERT INTO admin_action_log VALUES(1,'support_delivery',4,'sent',?)",
                       (json.dumps({'message_id': 9, 'telegram_message_id': 111, 'error_code': None}),))
        return path

    def test_valid_evidence_never_closes_manual_acceptance_or_changes_file(self):
        path = self.fixture()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = check.inspect(path, 17, 4)
        self.assertEqual(result['backend_checks'], 'ok')
        self.assertEqual((result['C05'], result['E01']), ('open', 'open'))
        self.assertEqual(result['manual_acceptance'], 'pending_owner_confirmation')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertNotIn('actor_id', json.dumps(result))
        self.assertNotIn(check.ANSWER, json.dumps(result))

    def test_wrong_records_or_same_account_do_not_pass(self):
        path = self.fixture()
        self.assertEqual(check.inspect(path, 999, 4)['backend_checks'], 'not_confirmed')
        with sqlite3.connect(path) as db:
            db.execute('UPDATE support_tickets SET requester_id=1')
        self.assertFalse(check.inspect(path, 17, 4)['E01_backend']['two_distinct_accounts'])

    def test_uncertain_delivery_or_mismatched_audit_does_not_pass(self):
        path = self.fixture()
        with sqlite3.connect(path) as db:
            db.execute("UPDATE support_reply_deliveries SET status='uncertain'")
        self.assertEqual(check.inspect(path, 17, 4)['backend_checks'], 'not_confirmed')
        with sqlite3.connect(path) as db:
            db.execute("UPDATE support_reply_deliveries SET status='sent',telegram_message_id=222")
        self.assertFalse(check.inspect(path, 17, 4)['E01_backend']['one_sent_audit'])

    def test_duplicate_marked_reply_or_wrong_reason_does_not_pass(self):
        path = self.fixture()
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO support_messages VALUES(10,4,1,1,?)', (check.ANSWER,))
            db.execute("UPDATE lead_lifecycle_events SET reason='old reason'")
        result = check.inspect(path, 17, 4)
        self.assertFalse(result['C05_backend']['one_closure_with_final_reason'])
        self.assertFalse(result['E01_backend']['one_marked_reply'])

    def test_matching_evidence_from_different_accounts_cannot_confirm_expected_pair(self):
        path = self.fixture()
        self.assertEqual(check.inspect(path, 17, 4, requester_id=20, admin_id=1)['backend_checks'], 'ok')
        for requester, admin in [(21, 1), (20, 2), (21, 2)]:
            result = check.inspect(path, 17, 4, requester_id=requester, admin_id=admin)
            self.assertEqual(result['backend_checks'], 'not_confirmed')
            self.assertEqual((result['C05'], result['E01']), ('open', 'open'))

    def test_expected_pair_is_not_disclosed_and_inspection_keeps_source_unchanged(self):
        path = self.fixture()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = check.inspect(path, 17, 4, requester_id=20, admin_id=1)
        self.assertTrue(all(result['expected_accounts'].values()))
        encoded = json.dumps(result)
        self.assertNotIn('requester_id', encoded)
        self.assertNotIn('admin_id', encoded)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
