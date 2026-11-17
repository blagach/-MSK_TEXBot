"""New-submission publication policy and owner pause in existing SQLite tables.

No scheduler, schema change, Telegram calls or historical backlog replay.
"""
import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import lead_routing
import storage
import user_access
import referral_storage


class SubmissionRejected(ValueError):
    pass


def validate(data, phone):
    lead_routing.validate(data.get("equipment"), data.get("region"), data.get("area"))
    if not storage.valid_text(data.get("place"), 300):
        raise SubmissionRejected("Укажите адрес работ, не более 300 символов. Черновик сохранён.")
    if not storage.valid_text(phone, 32, phone=True):
        raise SubmissionRejected("Проверьте телефон. Черновик сохранён.")
    if not isinstance(data.get("details", ""), str) or len(data.get("details", "")) > 1500:
        raise SubmissionRejected("Описание не должно превышать 1500 символов. Черновик сохранён.")


async def guard_submission(db, customer_id, data, *, now=None):
    """Call inside the submission's BEGIN IMMEDIATE to serialize distinct drafts."""
    now = now or datetime.now(timezone.utc)
    rows = await (await db.execute(
        "SELECT equipment,place,work_date,details,created_at FROM leads WHERE customer_id=?",
        (customer_id,),
    )).fetchall()
    recent = 0
    normalize = lambda x: " ".join((x or "").casefold().split())
    target = tuple(normalize(data.get(k)) for k in ("equipment", "place", "work_date", "details"))
    for equipment, place, work_date, details, created_at in rows:
        try:
            stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue  # Preserve unreadable legacy timestamps; never rewrite them.
        except (ValueError, AttributeError, TypeError):
            continue
        if stamp >= now - timedelta(hours=1):
            recent += 1
        if stamp >= now - timedelta(days=1) and tuple(map(normalize, (equipment, place, work_date, details))) == target:
            raise SubmissionRejected("Такая заявка уже сохранена. Откройте «Мои заявки». Черновик сохранён.")
    if recent >= 3:
        raise SubmissionRejected("Можно создать не более трёх заявок за час. Попробуйте позже. Черновик сохранён.")


async def mode_in(db):
    row = await (await db.execute(
        "SELECT id,result FROM admin_action_log WHERE action='autopublish_mode' ORDER BY id DESC LIMIT 1",
    )).fetchone()
    return {"revision": row[0] if row else 0, "enabled": row[1] == "enabled" if row else True}


async def mode():
    async with aiosqlite.connect(storage.DB_PATH) as db:
        return await mode_in(db)


async def set_mode(actor_id, owner_id, enabled, revision):
    if owner_id is None or actor_id != owner_id or type(enabled) is not bool or type(revision) is not int:
        raise PermissionError("Только владелец может менять автопубликацию")
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        current = await mode_in(db)
        if current["revision"] != revision:
            return False
        await db.execute(
            "INSERT INTO admin_action_log(actor_id,action,result,details,created_at) VALUES (?,?,?,?,?)",
            (actor_id, "autopublish_mode", "enabled" if enabled else "paused",
             json.dumps({"previous_revision": revision}), datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return True


async def activate_new(lead_id, actor_id, *, automatic=True):
    """Activate in SQLite once. No channel receipt, network or backlog replay.

    The same transaction serializes activation against the owner's pause.
    Historical publishing (uncertain channel send) is never reactivated.
    """
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        lead = await (await db.execute(
            "SELECT customer_id,status FROM leads WHERE id=?", (lead_id,),
        )).fetchone()
        if not lead or lead[1] != 'new':
            return False
        if automatic and lead[0] != actor_id:
            raise user_access.AccessDenied("Заявка другого заказчика")
        if not await user_access.allowed_in(db, lead[0], "requests"):
            raise user_access.AccessDenied("Создание заявок ограничено")
        if not (await mode_in(db))["enabled"]:
            return False
        cursor = await db.execute(
            "UPDATE leads SET status='published' WHERE id=? AND status='new'",
            (lead_id,),
        )
        if cursor.rowcount != 1:
            return False
        await storage.lead_journey.record_event(db, lead_id, "published", actor_id=actor_id)
        await referral_storage.first_lead(db, lead[0], lead_id)
        await db.execute(
            "INSERT INTO admin_action_log(actor_id,action,target_id,result,details,created_at) VALUES (?,?,?,?,?,?)",
            (actor_id, "lead_auto_publish" if automatic else "lead_publish", lead_id,
             "success", '{"surface":"bot"}', datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return True
