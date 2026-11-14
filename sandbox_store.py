"""Owned sandbox files; Render writes require a verified opt-in test scope."""
import os
import math
import sqlite3
import fcntl
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

MARKER = 'MSK_TEXBOT_LOCAL_SANDBOX_V1'


class SandboxBusy(RuntimeError):
    pass


def instant(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Expected a finite UTC timestamp')
    return float(value)


class SandboxStore:
    @staticmethod
    def resolve_path(path, test_scope=None):
        if test_scope is None:
            return SandboxStore.local_path(path)
        from sandbox_test_runtime import VerifiedTestScope
        if type(test_scope) is not VerifiedTestScope:
            raise RuntimeError('An authenticated test runtime scope is required')
        return test_scope.store_path(path)

    @staticmethod
    def local_path(path):
        path = Path(path)
        resolved = path.resolve()
        if os.getenv('RENDER_SERVICE_ID') or resolved == Path('/var/data') or Path('/var/data') in resolved.parents:
            raise RuntimeError('This sandbox is local only; Render and persistent service disks are forbidden')
        if path.is_symlink():
            raise ValueError('Symlink sandbox files are forbidden')
        return resolved

    @classmethod
    def create(cls, path, *, test_scope=None):
        path = cls.resolve_path(path, test_scope)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE sandbox_marker (value TEXT PRIMARY KEY)')
            db.execute('INSERT INTO sandbox_marker VALUES (?)', (MARKER,))
            db.commit()
        finally:
            db.close()
        return cls(path, test_scope=test_scope)

    def __init__(self, path, *, test_scope=None):
        self.test_scope = test_scope
        self.path = self.resolve_path(path, test_scope)
        self._held = threading.local()
        # Validate read-only before any schema or data operation.
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute('SELECT value FROM sandbox_marker').fetchall() != [(MARKER,)]:
                raise ValueError('Not an owned local sandbox')
        finally:
            db.close()

    @contextmanager
    def connection(self):
        self.validate()
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if [row[0] for row in db.execute('SELECT value FROM sandbox_marker')] != [MARKER]:
                raise ValueError('Sandbox identity changed')
            db.execute('PRAGMA foreign_keys=ON')
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def schema(self, sql):
        with self.connection() as db:
            db.executescript(sql)

    def validate(self):
        self.resolve_path(self.path, self.test_scope)

    @contextmanager
    def exclusive(self, name):
        """Kernel-owned coordination; a crashed process cannot retain the lock.

        The empty lock file is intentionally retained. No unlink, PID guessing,
        time-based takeover or stale-reservation resend is used.
        """
        if name not in ('campaign', 'checkout'):
            raise ValueError('Unknown sandbox lock')
        self.validate()
        lockpath = self.path.with_name(self.path.name+'.'+name+'.lock')
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe local coordination file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SandboxBusy('Another local operation owns the coordinator') from None
            acquired = True
            held = set(getattr(self._held, 'names', ()))
            held.add(name)
            self._held.names = held
            yield
        finally:
            if acquired:
                self._held.names = set(getattr(self._held, 'names', ())) - {name}
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def require_lock(self, name):
        if name not in getattr(self._held, 'names', ()):
            raise SandboxBusy('Coordinator must be held before this operation')
