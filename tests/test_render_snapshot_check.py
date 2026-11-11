"""Exercise the owner bootstrap without Render or network access."""
from pathlib import Path
from contextlib import redirect_stdout
import io
import json
import os
import pty
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import render_snapshot_check as check
import sqlite_rehearsal


class OwnerBootstrapTests(unittest.TestCase):
    def fixture(self):
        root = Path(tempfile.mkdtemp(prefix='msk-owner-bootstrap-'))
        source, restored = root / 'snapshot.sqlite', root / 'restored.sqlite'
        with sqlite3.connect(source) as db:
            db.execute('''CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
                customer_name TEXT, equipment TEXT NOT NULL, place TEXT NOT NULL,
                work_date TEXT NOT NULL, details TEXT NOT NULL, phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL)''')
            db.execute("INSERT INTO leads VALUES (11,20,'Private sentinel','автокран','Москва','завтра','','+79990000000','published','2026-11-11')")
            db.execute("CREATE TABLE form_drafts (storage_key TEXT PRIMARY KEY,state TEXT,data TEXT NOT NULL DEFAULT '{}')")
            db.execute("INSERT INTO form_drafts VALUES ('fixture','LeadForm:attachments','{\"sentinel\":11}')")
            db.execute('''CREATE TABLE admin_role_events (id INTEGER PRIMARY KEY,target_id INTEGER NOT NULL,
                role TEXT NOT NULL,actor_id INTEGER NOT NULL,request_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)''')
            db.execute("INSERT INTO admin_role_events VALUES (1,22,'none',1,'fixture-role','2026-11-11')")
            db.execute('''CREATE TABLE user_access_events (id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,
                scopes_json TEXT NOT NULL,reason TEXT NOT NULL,actor_id INTEGER NOT NULL,
                request_id TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL)''')
            db.execute("INSERT INTO user_access_events VALUES (1,30,'[\"full\"]','Fixture',1,'fixture-access','2026-11-11')")
            db.execute('''CREATE TABLE admin_action_log (id INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,target_id INTEGER,result TEXT NOT NULL,details TEXT NOT NULL,created_at TEXT NOT NULL)''')
            db.execute("INSERT INTO admin_action_log VALUES (1,1,'role_changed',22,'success','{}','2026-11-11')")
        with restored.open('xb') as file:
            file.write(source.read_bytes())
        return root, source, restored

    def test_public_repository_bootstrap_runs_only_fresh_copies(self):
        root, source, restored = self.fixture()
        checksum = check.digest(source)
        previous = root / 'owner-rehearsal-previous'
        previous.mkdir(mode=0o700)
        marker = previous / 'retained.txt'
        marker.write_bytes(b'previous attempt must remain')
        env = {'RENDER_SERVICE_ID': check.WORKER, 'RENDER_GIT_COMMIT': check.INSTALLED}
        with patch.object(check, 'SNAPSHOT', source), patch.object(check, 'RESTORED', restored), \
             patch.object(check, 'REPOSITORY', str(sqlite_rehearsal.ROOT)):
            result = check.run(checksum, env)
        self.assertEqual(result['result'], 'ok')
        self.assertEqual(result['passes'], 5)
        self.assertEqual(result['source_tables_verified'], 5)
        self.assertEqual(result['counts']['leads'], 1)
        self.assertEqual(result['existing_backups'], 'unchanged')
        self.assertFalse(result['worker_started'])
        self.assertFalse(result['deployed'])
        self.assertEqual(result['draft_states'], 'recognized')
        self.assertEqual(result['saved_roles_and_restrictions'], 'ok')
        self.assertEqual(result['counts']['admin_action_log'], 1)
        self.assertNotIn('Private sentinel', str(result))
        self.assertEqual(check.digest(source), checksum)
        self.assertEqual(check.digest(restored), checksum)
        self.assertTrue((Path(result['evidence']) / 'owner-result.json').is_file())
        self.assertNotEqual(Path(result['evidence']), previous)
        self.assertEqual(marker.read_bytes(), b'previous attempt must remain')

    def test_nonseekable_terminal_reproduction_and_explicit_checksum_entry(self):
        master, slave = pty.openpty()
        try:
            with self.assertRaisesRegex(io.UnsupportedOperation, 'not seekable'):
                with open(os.ttyname(slave), 'r+'):
                    pass
        finally:
            os.close(slave)
            os.close(master)
        checksum = '92555b1828b18f6e082fe33ef3e1774efb648b617926d05ab3413f01e59d2f7f'
        output = io.StringIO()
        env = {'RENDER_SERVICE_ID': check.WORKER, 'RENDER_GIT_COMMIT': check.INSTALLED}
        with patch.dict(os.environ, env), patch.object(Path, 'is_mount', return_value=True), \
             patch('builtins.open', side_effect=AssertionError('No terminal or stdin input permitted')), \
             patch.object(check, 'run', return_value={'result': 'ok'}) as run, redirect_stdout(output):
            check.main(checksum)
        run.assert_called_once_with(checksum)
        self.assertEqual(json.loads(output.getvalue()), {'result': 'ok'})

    def test_missing_explicit_checksum_stops_without_running_rehearsal(self):
        output = io.StringIO()
        env = {'RENDER_SERVICE_ID': check.WORKER, 'RENDER_GIT_COMMIT': check.INSTALLED}
        with patch.dict(os.environ, env), patch.object(Path, 'is_mount', return_value=True), \
             patch.object(check, 'run') as run, redirect_stdout(output):
            with self.assertRaises(SystemExit):
                check.main()
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['result'], 'STOP')

    def test_wrong_worker_or_commit_stops_before_any_file_or_process(self):
        with patch.object(check, 'digest') as digest, patch.object(check.subprocess, 'run') as run:
            for env in ({}, {'RENDER_SERVICE_ID': check.WORKER, 'RENDER_GIT_COMMIT': 'other'}):
                with self.assertRaises(RuntimeError):
                    check.run('0' * 64, env)
            digest.assert_not_called()
            run.assert_not_called()

    def test_bad_hash_does_not_create_files_or_download_code(self):
        root, source, restored = self.fixture()
        before = sorted(root.iterdir())
        env = {'RENDER_SERVICE_ID': check.WORKER, 'RENDER_GIT_COMMIT': check.INSTALLED}
        with patch.object(check, 'SNAPSHOT', source), patch.object(check, 'RESTORED', restored), \
             patch.object(check.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'SHA-256 mismatch'):
                check.run('0' * 64, env)
            run.assert_not_called()
        self.assertEqual(sorted(root.iterdir()), before)
