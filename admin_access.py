"""Permission checks and append-only administrator role and action history."""
import json
from datetime import datetime, timezone

import aiosqlite
import storage

PERMISSIONS = {
    "moderator": frozenset({"moderate", "stats", "catalog"}),
    "support": frozenset({"support"}),
    "finance": frozenset({"finance"}),
    "marketer": frozenset({"marketing"}),
    "administrator": frozenset({"moderate", "stats", "catalog", "support", "users", "export"}),
    "none": frozenset(),
}
ALL_PERMISSIONS = frozenset().union(*PERMISSIONS.values(), {"roles", "audit"})


def now():
    return datetime.now(timezone.utc).isoformat()


async def init_schema():
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS admin_role_events (
                id INTEGER PRIMARY KEY,
                target_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                request_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS admin_role_latest ON admin_role_events(target_id,id);
            CREATE TABLE IF NOT EXISTS admin_lead_views (
                admin_id INTEGER NOT NULL,
                lead_id INTEGER NOT NULL REFERENCES leads(id),
                created_at TEXT NOT NULL,
                PRIMARY KEY(admin_id, lead_id)
            );
            CREATE TABLE IF NOT EXISTS admin_action_log (
                id INTEGER PRIMARY KEY,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_id INTEGER,
                result TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        await db.commit()


async def role_in(db, user_id, legacy_admins, owner_id):
    if owner_id is not None and user_id == owner_id:
        return "owner"
    row = await (await db.execute(
        "SELECT role FROM admin_role_events WHERE target_id=? ORDER BY id DESC LIMIT 1", (user_id,),
    )).fetchone()
    return row[0] if row else ("administrator" if user_id in legacy_admins else "none")


class RoleConflict(RuntimeError):
    """The role was changed after the owner reviewed it."""


async def role_snapshot_in(db, user_id, legacy_admins, owner_id):
    row = await (await db.execute(
        "SELECT id,role FROM admin_role_events WHERE target_id=? ORDER BY id DESC LIMIT 1", (user_id,),
    )).fetchone()
    role = "owner" if user_id == owner_id else (row[1] if row else ("administrator" if user_id in legacy_admins else "none"))
    return {"role": role, "revision": row[0] if row else 0}


async def role_snapshot(user_id, legacy_admins=(), owner_id=None):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        return await role_snapshot_in(db, user_id, legacy_admins, owner_id)


async def allowed(user_id, permission, legacy_admins, owner_id=None):
    if permission != "panel" and permission not in ALL_PERMISSIONS:
        return False
    async with aiosqlite.connect(storage.DB_PATH) as db:
        role = await role_in(db, user_id, legacy_admins, owner_id)
    if permission == "panel":
        return role == "owner" or bool(PERMISSIONS.get(role))
    return role == "owner" or permission in PERMISSIONS.get(role, ())


async def staff(permission, legacy_admins, owner_id=None):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        ids = set(legacy_admins)
        ids.update(row[0] for row in await (await db.execute("SELECT DISTINCT target_id FROM admin_role_events")).fetchall())
        if owner_id is not None:
            ids.add(owner_id)
        result = []
        for user_id in sorted(ids):
            role = await role_in(db, user_id, legacy_admins, owner_id)
            if role == "owner" or permission in PERMISSIONS.get(role, ()):
                result.append(user_id)
        return result


async def record(actor_id, action, *, target_id=None, result="success", details=None):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute(
            "INSERT INTO admin_action_log(actor_id,action,target_id,result,details,created_at) VALUES (?,?,?,?,?,?)",
            (actor_id, action, target_id, result, json.dumps(details or {}, ensure_ascii=False), now()),
        )
        await db.commit()


async def assign_role(actor_id, target_id, role, request_id, *, owner_id, expected_revision=None, legacy_admins=()):
    if owner_id is None or actor_id != owner_id:
        raise PermissionError("Только владелец может назначать права")
    if type(target_id) is not int or target_id <= 0 or target_id == owner_id:
        raise ValueError("Нельзя изменять роль владельца")
    if role not in PERMISSIONS or not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        raise ValueError("Некорректная роль или запрос")
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        previous = await (await db.execute(
            "SELECT actor_id,target_id,role FROM admin_role_events WHERE request_id=?", (request_id,),
        )).fetchone()
        if previous:
            if previous != (actor_id, target_id, role):
                raise ValueError("Повторный запрос содержит другие данные")
            return False
        previous_role = await role_snapshot_in(db, target_id, legacy_admins, owner_id)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision != previous_role["revision"]
        ):
            await db.execute(
                "INSERT INTO admin_action_log(actor_id,action,target_id,result,details,created_at) VALUES (?,?,?,?,?,?)",
                (actor_id, "role_changed", target_id, "conflict", json.dumps({
                    "expected_revision": expected_revision, "actual_revision": previous_role["revision"],
                }), now()),
            )
            await db.commit()
            raise RoleConflict("Права изменились после просмотра. Проверьте текущую роль заново")
        stamp = now()
        await db.execute("INSERT INTO admin_role_events(target_id,role,actor_id,request_id,created_at) VALUES (?,?,?,?,?)",
                         (target_id, role, actor_id, request_id, stamp))
        await db.execute(
            "INSERT INTO admin_action_log(actor_id,action,target_id,result,details,created_at) VALUES (?,?,?,?,?,?)",
            (actor_id, "role_changed", target_id, "success", json.dumps({"role": role, "previous_role": previous_role["role"],
                                                  "previous_revision": previous_role["revision"]}), stamp),
        )
        await db.commit()
        return True


async def history(before=2**63 - 1, limit=10):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM admin_action_log WHERE id<? ORDER BY id DESC LIMIT ?", (before, limit),
        )).fetchall()


async def mark_lead_view(user_id, lead_id):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute(
            "INSERT INTO admin_lead_views VALUES (?,?,?) ON CONFLICT(admin_id,lead_id) DO NOTHING",
            (user_id, lead_id, now()),
        )
        await db.commit()


async def administrator_page(legacy_admins, owner_id=None, *, after=0, limit=10):
    """Read a consistent page without changing or hiding past assignments."""
    configured = sorted(set(legacy_admins) | ({owner_id} if owner_id is not None else set()))
    union = "".join(" UNION SELECT ?" for _ in configured)
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute("BEGIN")
        candidates = await (await db.execute(
            "WITH candidates(id) AS (SELECT DISTINCT target_id FROM admin_role_events" + union +
            ") SELECT id FROM candidates WHERE id>? ORDER BY id LIMIT ?",
            (*configured, after, min(max(limit, 1), 50)),
        )).fetchall()
        result = []
        for (user_id,) in candidates:
            result.append({"id": user_id, **await role_snapshot_in(db, user_id, legacy_admins, owner_id)})
        return result
