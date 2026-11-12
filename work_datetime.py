"""Explicit Moscow work times; never modifies historical database values."""
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo('Europe/Moscow')


def parse_work_datetime(value, *, now=None):
    now = now or datetime.now(MOSCOW)
    if now.tzinfo is None:
        raise ValueError('Timezone-aware clock required')
    now = now.astimezone(MOSCOW)
    value = value.strip().lower()
    relative = re.fullmatch(r'(сегодня|завтра)\s+(\d{1,2}):([0-5]\d)', value)
    if relative:
        day = now.date() + timedelta(days=relative[1] == 'завтра')
        result = datetime(day.year, day.month, day.day, int(relative[2]), int(relative[3]), tzinfo=MOSCOW)
    else:
        result = datetime.strptime(value, '%d.%m.%Y %H:%M').replace(tzinfo=MOSCOW)
    if result <= now:
        raise ValueError('Work time must be in the future')
    return result.strftime('%d.%m.%Y %H:%M')
