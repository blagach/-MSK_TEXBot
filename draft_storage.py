"""SQLite-backed FSM for the isolated Render worker."""
import json
from typing import Any, Mapping

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder, StateType, StorageKey


class SQLiteDraftStorage(BaseStorage):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.keys = DefaultKeyBuilder(with_bot_id=True, with_business_connection_id=True, with_destiny=True)

    async def init_schema(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS form_drafts (
                storage_key TEXT PRIMARY KEY,
                state TEXT,
                data TEXT NOT NULL DEFAULT '{}'
            )""")
            await db.execute("""CREATE TABLE IF NOT EXISTS lead_draft_submissions (
                draft_id TEXT PRIMARY KEY,
                lead_id INTEGER NOT NULL REFERENCES leads(id)
            )""")
            await db.commit()

    async def _read(self, db: aiosqlite.Connection, key: StorageKey) -> tuple[str | None, dict]:
        async with db.execute(
            "SELECT state, data FROM form_drafts WHERE storage_key = ?", (self.keys.build(key),)
        ) as cursor:
            row = await cursor.fetchone()
        return (row[0], json.loads(row[1])) if row else (None, {})

    async def _write(self, db: aiosqlite.Connection, key: StorageKey, state: StateType, data: Mapping) -> None:
        await db.execute(
            """INSERT INTO form_drafts (storage_key, state, data) VALUES (?, ?, ?)
               ON CONFLICT(storage_key) DO UPDATE SET state=excluded.state, data=excluded.data""",
            (self.keys.build(key), state.state if isinstance(state, State) else state, json.dumps(dict(data))),
        )

    async def checkpoint(self, key: StorageKey, state: StateType, data: Mapping) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await self._write(db, key, state, data)
            await db.commit()

    async def transfer(self, source: StorageKey, destination: StorageKey) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            state, data = await self._read(db, source)
            if state is not None:
                data.pop("form_keyboard_id", None)
                await self._write(db, destination, state, data)
                await self._write(db, source, None, {})
            await db.commit()
            return state

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            _, data = await self._read(db, key)
            await self._write(db, key, state, data)
            await db.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            state, _ = await self._read(db, key)
            return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            state, _ = await self._read(db, key)
            await self._write(db, key, state, data)
            await db.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            _, data = await self._read(db, key)
            return data

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            state, current = await self._read(db, key)
            current.update(data)
            await self._write(db, key, state, current)
            await db.commit()
            return current

    async def close(self) -> None:
        # Connections are scoped to operations.
        pass
