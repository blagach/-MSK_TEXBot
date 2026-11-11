"""Rehearse exact Git revisions on a new copy of a verified SQLite snapshot."""
import argparse
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

from render_test_backup import backup_database, open_read_only, verify_preserved_rows

ROOT = Path(__file__).resolve().parent
PROBE = r'''
import asyncio, os, pathlib, sys
target = pathlib.Path(sys.argv[1]).resolve(strict=True)
def guard(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'}:
        raise RuntimeError('External activity forbidden in SQLite rehearsal')
    if event == 'sqlite3.connect':
        if str(args[0]) != str(target):
            raise RuntimeError('SQLite access outside the new rehearsal copy')
sys.addaudithook(guard)
import bot, storage
storage.DB_PATH = str(target)
async def run():
    await bot.init_db()
    await bot.SQLiteDraftStorage(str(target)).init_schema()
asyncio.run(run())
'''


def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def extract_revision(repo, revision, destination):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('A full exact commit SHA is required')
    actual = subprocess.check_output(
        ['git', '-C', str(repo), 'rev-parse', revision + '^{commit}'], text=True,
    ).strip()
    if actual != revision:
        raise ValueError('Unexpected commit identity')
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', revision])
    destination.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive)) as contents:
        contents.extractall(destination, filter='data')


def rehearse(snapshot, checksum, candidate_sha, rollback_sha, *, repo=ROOT, evidence_parent=None):
    snapshot = Path(snapshot)
    if snapshot.is_symlink() or not snapshot.is_file() or snapshot.name == 'bot.db':
        raise ValueError('Use a regular snapshot file, never the live bot.db')
    snapshot = snapshot.resolve(strict=True)
    if not re.fullmatch(r'[0-9a-f]{64}', checksum) or digest(snapshot) != checksum:
        raise ValueError('Snapshot SHA-256 mismatch')
    if evidence_parent is not None:
        evidence_parent = Path(evidence_parent).resolve(strict=True)
        if not evidence_parent.is_dir():
            raise ValueError('Evidence parent must be an existing directory')
    evidence = Path(tempfile.mkdtemp(prefix='sqlite-rehearsal-', dir=evidence_parent))
    print('Evidence retained at ' + str(evidence), flush=True)
    candidate_code, rollback_code = evidence / 'candidate-code', evidence / 'rollback-code'
    extract_revision(repo, candidate_sha, candidate_code)
    extract_revision(repo, rollback_sha, rollback_code)
    working = evidence / 'working.sqlite'
    backup_database(snapshot, working)
    environment = {
        'PATH': os.defpath, 'BOT_TOKEN': '123456:TEST', 'ADMIN_IDS': '1,2',
        'LEADS_CHANNEL_ID': '-100123', 'PYTHON_DOTENV_DISABLED': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    stages = []
    migrated = evidence / 'migrated-baseline.sqlite'
    for index, code in enumerate((candidate_code, candidate_code, rollback_code, rollback_code, candidate_code)):
        subprocess.run([sys.executable, '-B', '-c', PROBE, str(working)], cwd=code,
                       env=environment, check=True, timeout=120, capture_output=True)
        count = verify_preserved_rows(snapshot, working)
        if index == 0:
            backup_database(working, migrated)
        else:
            verify_preserved_rows(migrated, working)
        if digest(snapshot) != checksum:
            raise RuntimeError('Source snapshot changed during rehearsal')
        stages.append({'stage': index + 1, 'revision': candidate_sha if code == candidate_code else rollback_sha,
                       'source_tables_verified': count})
    with closing(open_read_only(working)) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        counts = {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]
                  for name in tables}
    report = {'snapshot': str(snapshot), 'snapshot_sha256': checksum, 'candidate': candidate_sha,
              'rollback': rollback_sha, 'working_copy': str(working), 'stages': stages,
              'integrity': 'ok', 'foreign_keys': 'ok', 'all_source_rows': 'preserved',
              'migrated_rows': 'preserved', 'counts': counts,
              'scope': 'schema initialization and full row preservation; no Worker or functional acceptance'}
    with (evidence / 'report.json').open('x') as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--rollback', required=True)
    parser.add_argument('--evidence-parent', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(rehearse(args.snapshot, args.sha256, args.candidate, args.rollback,
                              evidence_parent=args.evidence_parent), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
