"""Serialize closure/restoration and each Telegram attempt in the single Worker.

Never hold a SQLite transaction across a network call. Multiple bot processes
against the same database are unsupported; durable reservations still prevent
replay after a process restart. An already accepted request cannot be recalled.
"""
import asyncio
import os
import weakref

_locks = weakref.WeakValueDictionary()


def for_lead(db_path, lead_id):
    key = (asyncio.get_running_loop(), os.path.realpath(db_path), lead_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock
