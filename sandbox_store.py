"""Exclusive local sandbox files. Never imported by the bot or a Render worker."""
import os
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path

MARKER = 'MSK_TEXBOT_LOCAL_SANDBOX_V1'


def instant(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Expected a finite UTC timestamp')
    return float(value)


class SandboxStore:
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
    def create(cls, path):
        path = cls.local_path(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE sandbox_marker (value TEXT PRIMARY KEY)')
            db.execute('INSERT INTO sandbox_marker VALUES (?)', (MARKER,))
            db.commit()
        finally:
            db.close()
        return cls(path)

    def __init__(self, path):
        self.path = self.local_path(path)
        # Validate read-only before any schema or data operation.
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)
        try:
            if db.execute('SELECT value FROM sandbox_marker').fetchall() != [(MARKER,)]:
                raise ValueError('Not an owned local sandbox')
        finally:
            db.close()

    @contextmanager
    def connection(self):
        self.local_path(self.path)
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
