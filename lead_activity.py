"""Read-only lead activity checks. No Telegram sends or database mutations."""
from datetime import datetime, timedelta, timezone
import aiosqlite

def normalize(value):
    return " ".join(value.casefold().split())

async def activity(customer_id, *, equipment, place, work_date, details,
                   db_path="bot.db", now=None, window_minutes=60):
    if not isinstance(customer_id, int) or customer_id <= 0:
        raise ValueError("Invalid customer")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Timezone-aware clock required")
    count, duplicate = 0, False
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT equipment,place,work_date,details,created_at FROM leads WHERE customer_id=? AND status != 'rejected'",
            (customer_id,),
        ) as cursor:
            async for row in cursor:
                try:
                    stamp = datetime.fromisoformat(row[4].replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError("Naive timestamp")
                except (ValueError, AttributeError, TypeError):
                    raise ValueError("Invalid history: manual review required")
                if stamp < now - timedelta(hours=24) or stamp > now + timedelta(minutes=1):
                    continue
                if stamp >= now - timedelta(minutes=window_minutes):
                    count += 1
                if tuple(map(normalize, row[:4])) == tuple(
                    map(normalize, (equipment, place, work_date, details))
                ):
                    duplicate = True
    return count, duplicate
