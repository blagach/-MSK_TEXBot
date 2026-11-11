"""Retained old-schema fixtures for offline migration/rollback rehearsals."""
import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import admin_access
import storage
import user_access
import sqlite_rehearsal as rehearsal

OLD = '31d2e0170451fb85ebd7c986ff777cb675881104'
CANDIDATE = 'a5f4eb98877c56fe384b55ff212081af485dee2c'
ROLLBACK = 'cec41d17431c458aafb54242397116c419246848'


class SnapshotRehearsalTests(unittest.TestCase):
    def test_installed_schema_survives_five_exact_revision_passes(self):
        evidence = Path(tempfile.mkdtemp(prefix='msk-rehearsal-test-'))
        old_code = evidence / 'installed-code'
        rehearsal.extract_revision(rehearsal.ROOT, OLD, old_code)
        source = evidence / 'snapshot.sqlite'
        source.touch(mode=0o600)
        env = {'PATH': os.defpath, 'BOT_TOKEN': '123456:TEST', 'ADMIN_IDS': '1,2',
               'LEADS_CHANNEL_ID': '-100123', 'PYTHON_DOTENV_DISABLED': '1'}
        subprocess.run([sys.executable, '-B', '-c', rehearsal.PROBE, str(source)],
                       cwd=old_code, env=env, check=True, capture_output=True, timeout=60)
        with sqlite3.connect(source) as db:
            db.execute("INSERT INTO leads VALUES (11,20,'Fixture','автокран','Москва','завтра','','+79990000000','published','2026-11-11')")
            db.execute("INSERT INTO offers VALUES (1,11,30,'Fixture','test','32000','','+79990000001','2026-11-11','sent')")
            db.execute("INSERT INTO bot_visitors VALUES (20)")
            db.execute("INSERT INTO providers VALUES (30,'Fixture','автокран','Москва','+79990000001','2026-11-11')")
            db.execute("INSERT INTO form_drafts VALUES ('fixture','LeadForm.attachments','{\"sentinel\":11}')")
        async def seed_audit():
            with patch.object(storage, 'DB_PATH', str(source)):
                await admin_access.init_schema()
                await user_access.init_schema()
                await admin_access.assign_role(1, 2, 'none', 'fixture-revoke', owner_id=1)
                await user_access.change(1, 30, ['full'], 'Fixture restriction', 'fixture-access',
                                         expected_revision=0, owner_id=1, legacy_admins={1, 2})
        asyncio.run(seed_audit())
        checksum = rehearsal.digest(source)
        reference = evidence / 'existing-restored.sqlite'
        rehearsal.backup_database(source, reference)
        reference_hash = rehearsal.digest(reference)
        report = rehearsal.rehearse(source, checksum, CANDIDATE, ROLLBACK, evidence_parent=evidence)
        self.assertEqual(rehearsal.digest(source), checksum)
        self.assertEqual(rehearsal.digest(reference), reference_hash)
        self.assertEqual(len(report['stages']), 5)
        self.assertEqual(report['all_source_rows'], 'preserved')
        for table in ('leads', 'offers', 'providers', 'form_drafts', 'bot_visitors'):
            self.assertEqual(report['counts'][table], 1)
        self.assertGreater(report['counts']['admin_action_log'], 0)
        self.assertEqual(report['counts']['admin_role_events'], 1)
        self.assertEqual(report['counts']['user_access_events'], 1)
        self.assertTrue((Path(report['working_copy']).parent / 'report.json').is_file())
        print('Retained rehearsal fixture: ' + str(evidence))

    def test_bad_checksum_and_live_filename_are_rejected_before_creating_evidence(self):
        evidence = Path(tempfile.mkdtemp(prefix='msk-rehearsal-reject-'))
        source = evidence / 'snapshot.sqlite'
        source.touch(mode=0o600)
        before = sorted(evidence.iterdir())
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            rehearsal.rehearse(source, '0' * 64, CANDIDATE, ROLLBACK, evidence_parent=evidence)
        self.assertEqual(sorted(evidence.iterdir()), before)
        live = evidence / 'bot.db'
        live.touch(mode=0o600)
        with self.assertRaisesRegex(ValueError, 'never the live'):
            rehearsal.rehearse(live, rehearsal.digest(live), CANDIDATE, ROLLBACK, evidence_parent=evidence)

    def test_child_cannot_open_another_database_or_connect_to_network(self):
        evidence = Path(tempfile.mkdtemp(prefix='msk-rehearsal-guard-'))
        target = evidence / 'working.sqlite'
        target.touch(mode=0o600)
        guard = rehearsal.PROBE.split('import bot, storage')[0]
        for forbidden in ("import sqlite3; sqlite3.connect('/var/data/bot.db')",
                          "import socket; socket.getaddrinfo('example.com',443)"):
            result = subprocess.run([sys.executable, '-B', '-c', guard + '\n' + forbidden, str(target)],
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'RuntimeError', result.stderr)
