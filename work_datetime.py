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
    match = re.fullmatch(r'(сегодня|завтра|\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2})[:.]([0-5]\d)', value)
    if not match:
        if re.search(r'после|утра|вечера|около', value):
            raise ValueError('Нужно точное время: вместо «после 9 утра» укажите 09:00, если работа начинается в 9.')
        raise ValueError('Не удалось распознать дату и время. Пример: завтра 09:00 или 13.11.2026 09.00.')
    try:
        day = (now.date() + timedelta(days=match[1] == 'завтра') if match[1] in ('сегодня', 'завтра')
               else datetime.strptime(match[1], '%d.%m.%Y').date())
        result = datetime(day.year, day.month, day.day, int(match[2]), int(match[3]), tzinfo=MOSCOW)
    except ValueError as exc:
        raise ValueError('Несуществующая дата или время. Часы: 00–23, минуты: 00–59.') from exc
    if result <= now:
        raise ValueError('Дата и время уже прошли по Москве. Укажите будущее время, например завтра 09:00.')
    return result.strftime('%d.%m.%Y %H:%M')
