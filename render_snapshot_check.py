"""Owner-run bootstrap: verify the named test snapshot without updating Worker."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

WORKER = 'srv-dboqc51srm7s73cppi00'
INSTALLED = '31d2e0170451fb85ebd7c986ff777cb675881104'
TOOLS = '22518ef78905157c14ce44a71fce659b3e0236bb'
CANDIDATE = 'a5f4eb98877c56fe384b55ff212081af485dee2c'
ROLLBACK = 'cec41d17431c458aafb54242397116c419246848'
SNAPSHOT = Path('/var/data/backups/test-20261111T165141677395Z.sqlite')
RESTORED = Path('/var/data/backups/restore-candidate-20261111T165141687887Z.sqlite')
REPOSITORY = 'https://github.com/blagach/-MSK_TEXBot.git'
READ_CHECKS = r'''
import json, sqlite3
import admin_access, admin_ui, provider_ui, support_ui, user_access
from aiogram.fsm.state import StatesGroup
known = set()
for module in (bot, admin_ui, provider_ui, support_ui):
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, StatesGroup):
            known.update(state.state for state in value.__all_states__)
with sqlite3.connect(str(target)) as db:
    for state, data in db.execute('SELECT state,data FROM form_drafts'):
        if state is not None and state not in known:
            raise RuntimeError('Unrecognized persisted draft state')
        if not isinstance(json.loads(data), dict):
            raise RuntimeError('Persisted draft data is not an object')
async def check_policies():
    import aiosqlite
    async with aiosqlite.connect(str(target)) as db:
        roles = await (await db.execute("""SELECT target_id,role FROM admin_role_events e
            WHERE id=(SELECT max(id) FROM admin_role_events WHERE target_id=e.target_id)""")).fetchall()
        for actor, role in roles:
            if role not in admin_access.PERMISSIONS:
                raise RuntimeError('Unrecognized persisted role')
            for permission in admin_access.ALL_PERMISSIONS:
                if await admin_access.allowed(actor, permission, (), None) != (permission in admin_access.PERMISSIONS[role]):
                    raise RuntimeError('Persisted role policy changed')
        users = await (await db.execute('SELECT DISTINCT user_id FROM user_access_events')).fetchall()
        for (actor,) in users:
            saved = await user_access.snapshot_in(db, actor)
            for scope in ('requests', 'offers', 'notifications'):
                expected = 'full' not in saved['scopes'] and scope not in saved['scopes']
                if await user_access.allowed_in(db, actor, scope) != expected:
                    raise RuntimeError('Persisted access policy changed')
asyncio.run(check_policies())
'''
CHILD = r'''
from contextlib import redirect_stdout
import json, sys
from pathlib import Path
import sqlite_rehearsal
snapshot, checksum, candidate, rollback, repo, parent, reader = sys.argv[1:]
sqlite_rehearsal.PROBE += reader
with redirect_stdout(sys.stderr):
    report = sqlite_rehearsal.rehearse(Path(snapshot), checksum, candidate, rollback,
                                      repo=Path(repo), evidence_parent=Path(parent))
print(json.dumps(report))
'''


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def preflight(checksum, env):
    require(env.get('RENDER_SERVICE_ID') == WORKER, 'Wrong test Worker')
    require(env.get('RENDER_GIT_COMMIT') == INSTALLED, 'Installed commit changed')
    require(sys.version_info >= (3, 12), 'Python 3.12 or newer required')
    require(re.fullmatch('[0-9a-f]{64}', checksum) is not None, 'Expected 64 hexadecimal SHA-256 characters')
    require(not SNAPSHOT.is_symlink() and SNAPSHOT.is_file(), 'Snapshot missing or symlink')
    require(not RESTORED.is_symlink() and RESTORED.is_file(), 'Existing restore file missing or symlink')
    parent = SNAPSHOT.parent
    require(not parent.is_symlink() and parent.is_dir(), 'Invalid backups directory')
    info = parent.stat()
    require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0,
            'Backups directory must be private and owned by this user')
    require(digest(SNAPSHOT) == checksum, 'Snapshot SHA-256 mismatch')
    require(digest(RESTORED) == checksum, 'Existing restored file SHA-256 mismatch')
    require(shutil.disk_usage(parent).free > 3 * SNAPSHOT.stat().st_size + 32 * 1024 * 1024,
            'Insufficient free space for retained rehearsal copies')
    require(shutil.which('git') is not None, 'Git is unavailable; nothing installed')
    for name in ('aiogram', 'aiosqlite', 'dotenv', 'openpyxl'):
        importlib.import_module(name)


def run(checksum, env=None):
    checksum = checksum.strip().lower()
    preflight(checksum, os.environ if env is None else env)
    os.umask(0o077)
    evidence = Path(tempfile.mkdtemp(prefix='owner-rehearsal-', dir=SNAPSHOT.parent))
    # Only the bootstrap accesses the public repository. No SQLite is uploaded.
    clean_env = {'PATH': os.environ.get('PATH', os.defpath), 'GIT_TERMINAL_PROMPT': '0',
                 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHON_DOTENV_DISABLED': '1'}
    repo, code = evidence / 'repository', evidence / 'tools'
    try:
        subprocess.run(['git', 'clone', '--quiet', '--no-checkout', REPOSITORY, str(repo)],
                       env=clean_env, check=True, timeout=120, capture_output=True)
        actual = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', TOOLS + '^{commit}'],
                                         env=clean_env, text=True).strip()
        require(actual == TOOLS, 'Unexpected tool commit')
        code.mkdir(mode=0o700)
        # Load only the two reviewed helper modules into a new empty directory.
        for name in ('sqlite_rehearsal.py', 'render_test_backup.py'):
            content = subprocess.check_output(['git', '-C', str(repo), 'show', TOOLS + ':' + name],
                                              env=clean_env)
            with (code / name).open('xb') as file:
                file.write(content)
        process = subprocess.run(
            [sys.executable, '-B', '-c', CHILD, str(SNAPSHOT), checksum, CANDIDATE,
             ROLLBACK, str(repo), str(evidence), READ_CHECKS], cwd=code, env=clean_env,
            check=True, capture_output=True, text=True, timeout=300)
        report = json.loads(process.stdout)
        require(digest(SNAPSHOT) == checksum and digest(RESTORED) == checksum,
                'An existing backup changed')
        result = {'result': 'ok', 'worker_id': WORKER, 'installed': INSTALLED,
                  'candidate': CANDIDATE, 'rollback': ROLLBACK, 'snapshot_sha256': checksum,
                  'source_tables_verified': report['stages'][0]['source_tables_verified'],
                  'passes': len(report['stages']), 'integrity': report['integrity'],
                  'foreign_keys': report['foreign_keys'], 'all_source_rows': report['all_source_rows'],
                  'migrated_rows': report['migrated_rows'], 'existing_backups': 'unchanged',
                  'draft_states': 'recognized', 'saved_roles_and_restrictions': 'ok',
                  'counts': report['counts'], 'evidence': str(evidence),
                  'working_copy': report['working_copy'], 'worker_started': False, 'deployed': False,
                  'acceptance': 'C05/E01 remain open; functional acceptance is separate'}
        with (evidence / 'owner-result.json').open('x') as file:
            json.dump(result, file, indent=2)
        return result
    except Exception as error:
        # Do not print captured output: it may originate from application imports.
        raise RuntimeError('Check stopped (' + type(error).__name__ + '); files retained at ' + str(evidence)) from None


def main():
    try:
        require(os.environ.get('RENDER_SERVICE_ID') == WORKER, 'Wrong test Worker')
        require(os.environ.get('RENDER_GIT_COMMIT') == INSTALLED, 'Installed commit changed')
        disk = Path('/var/data')
        require(not disk.is_symlink() and disk.is_mount(), 'Test disk is not mounted at /var/data')
        with open('/dev/tty', 'r+') as terminal:
            terminal.write('Paste snapshot_sha256 from the previous JSON, then Enter: ')
            terminal.flush()
            checksum = terminal.readline()
        print(json.dumps(run(checksum), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'result': 'STOP', 'reason': str(error)}, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
