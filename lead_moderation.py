"""Conservative, side-effect-free moderation decisions for the pilot.

No Telegram sends, deletions or changes to the existing lead schema.
Only a previously approved customer may be auto-eligible; all other
cases require review. Eligibility is not permission to publish.
"""
import re
from dataclasses import dataclass

LINK = re.compile(r"(?:https?://|www\\.|t\\.me/|@[a-zA-Z0-9_]{5,})", re.I)

@dataclass(frozen=True)
class Decision:
    status: str
    reason: str

def assess(*, previously_approved: bool, category: str, region: str,
           area: str, place: str, work_date: str, details: str,
           recent_requests: int = 0, duplicate: bool = False) -> Decision:
    if duplicate:
        return Decision("review", "Возможный повтор заявки")
    if recent_requests >= 3:
        return Decision("review", "Слишком частые заявки")
    if any(not isinstance(x, str) or not x.strip()
           for x in (category, region, area, place, work_date, details)):
        return Decision("review", "Не заполнены обязательные поля")
    if LINK.search(details) or LINK.search(place):
        return Decision("review", "Ссылка или реклама в заявке")
    if not previously_approved:
        return Decision("review", "Первая заявка заказчика")
    return Decision("eligible", "Проверенный заказчик; базовые проверки пройдены")
