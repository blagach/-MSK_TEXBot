"""Read-only shadow evaluation from persisted history. Never sends messages."""
import moderation_history
from lead_activity import activity
from auto_publish_shadow import decide, ShadowResult

async def evaluate(customer_id, *, equipment, region, area, place, work_date,
                   details, db_path="bot.db", enabled=False, now=None, exclude_lead_id=None):
    try:
        approved = await moderation_history.approved_before(customer_id, db_path)
        count, duplicate = await activity(
            customer_id, equipment=equipment, place=place,
            work_date=work_date, details=details, db_path=db_path, now=now,
            exclude_lead_id=exclude_lead_id,
        )
        return decide(
            enabled=enabled, approved=approved, duplicate=duplicate,
            recent_requests=count, category=equipment, region=region,
            area=area, place=place, work_date=work_date, details=details,
        )
    except Exception:
        return ShadowResult("manual", "История недоступна: требуется проверка администратора")
