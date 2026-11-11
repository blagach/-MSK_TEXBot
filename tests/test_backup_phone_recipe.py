"""The exact copy-paste backup recipe is executable and fails closed on another Worker."""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import render_test_backup as backup
import render_test_start as launcher


def recipe():
    document=Path(__file__).resolve().parents[1]/'docs/tz7-phone-backup-check.md'
    block=document.read_text().split('```bash\n',1)[1].split('```',1)[0].strip().splitlines()
    assert block[0]=="python - <<'PY'" and block[-1]=='PY'
    return compile('\n'.join(block[1:-1]),str(document),'exec')


class PhoneBackupRecipeTests(unittest.TestCase):
    def test_recipe_restores_exact_rows_and_reports_no_contacts(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'isolated.sqlite'
            with sqlite3.connect(source) as db:
                db.executescript('CREATE TABLE leads(id INTEGER PRIMARY KEY);'
                    'CREATE TABLE offers(id INTEGER PRIMARY KEY,lead_id INTEGER REFERENCES leads(id));'
                    'CREATE TABLE provider_profiles(telegram_id INTEGER PRIMARY KEY);'
                    'INSERT INTO leads VALUES(8),(9),(11),(14);'
                    'INSERT INTO offers VALUES(1,11); INSERT INTO provider_profiles VALUES(42);')
            output=io.StringIO()
            with patch.dict(os.environ,{'RENDER_SERVICE_ID':launcher.TEST_WORKER_ID}), \
                 patch.object(launcher,'test_database_path',return_value=source),redirect_stdout(output):
                exec(recipe(),{})
            result=json.loads(output.getvalue())
            self.assertEqual(result['counts'],{'leads':4,'offers':1,'provider_profiles':1})
            self.assertTrue(all(result['lead_ids_present'].values()))
            self.assertEqual(result['source_tables_verified'],3)
            self.assertEqual(result['integrity'],'ok')
            self.assertEqual(result['schema'],'ok')
            self.assertNotEqual(result['snapshot'],str(source))
            self.assertNotEqual(result['restored'],str(source))
            self.assertEqual(backup.verify_preserved_rows(Path(result['snapshot']),source),3)
            self.assertEqual(backup.verify_preserved_rows(Path(result['snapshot']),Path(result['restored'])),3)
            self.assertNotIn('BOT_TOKEN',output.getvalue())

    def test_another_worker_stops_before_database_or_snapshot_access(self):
        with patch.dict(os.environ,{'RENDER_SERVICE_ID':'not-the-test-worker'}), \
             patch.object(launcher,'test_database_path') as database,patch.object(backup,'snapshot') as snapshot:
            with self.assertRaises(SystemExit):
                exec(recipe(),{})
            database.assert_not_called()
            snapshot.assert_not_called()
