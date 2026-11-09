"""Safe recipient selection for future targeted lead notifications.

No Telegram messages are sent by this module. A lead must have structured
category, region and area before it can be matched. Free-text leads are not
guessed or broadcast to everyone.
"""
import aiosqlite

import provider_registry
import waste_requests


async def recipients_for_lead(*, category, region, area, db_path=provider_registry.DEFAULT_DB_PATH):
    if category not in provider_registry.CATEGORIES:
        return []
    if region not in provider_registry.REGIONS:
        return []
    if not isinstance(area, str) or not area.strip():
        return []
    return await provider_registry.matching_providers(
        category, region, area.strip(), db_path=db_path
    )


def service_direction(category, waste_kind=None, hazard_class=None):
    if category in provider_registry.SERVICE_DIRECTIONS:
        return category
    if category != waste_requests.CATEGORY:
        return None
    if waste_kind == "construction" and hazard_class != "V":
        # No verified license data is available in the pilot.
        return None
    return waste_requests.DIRECTIONS.get(waste_kind)


async def recipients_for_service(*, direction, region, area, db_path=provider_registry.DEFAULT_DB_PATH):
    if direction not in provider_registry.SERVICE_DIRECTIONS or region not in provider_registry.REGIONS:
        return []
    if not isinstance(area, str) or not area.strip():
        return []
    # The profile has no service-specific territory yet. Require a declared
    # territory from active equipment as well as the selected service.
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            """SELECT p.telegram_id FROM provider_profiles p
               JOIN provider_services s ON s.provider_id=p.telegram_id
               WHERE p.blocked=0 AND p.notifications_enabled=1
                 AND s.selected=1 AND s.direction=?
                 AND EXISTS (
                    SELECT 1 FROM provider_equipment e
                    WHERE e.provider_id=p.telegram_id AND e.active=1 AND e.region IN (?,?)
                      AND e.area IN (?, 'весь регион')
                 )
               ORDER BY p.telegram_id""",
            (direction, region, provider_registry.BOTH_REGIONS, area.strip()),
        )).fetchall()
        return [row[0] for row in rows]


def notification_text(*, lead_id, category, region, area, work_date, details, bot_username, service=False):
    """Only public lead details and bot deep link; never customer phone."""
    if not isinstance(lead_id, int) or lead_id <= 0:
        raise ValueError("Invalid lead id")
    categories = provider_registry.SERVICE_DIRECTIONS if service else provider_registry.CATEGORIES
    if category not in categories or region not in provider_registry.REGIONS:
        raise ValueError("Unrecognized lead category or region")
    if not isinstance(area, str) or not area.strip():
        raise ValueError("Missing lead area")
    username = bot_username.lstrip("@")
    if not username or not username.replace("_", "").isalnum():
        raise ValueError("Invalid bot username")
    return (
        f"🚜 Подходящая заявка №{lead_id}\n"
        f"{'Услуга' if service else 'Техника'}: {category}\n"
        f"Регион: {region}, {area.strip()}\n"
        f"Дата: {work_date}\n"
        f"Описание: {details}\n"
        f"Откликнуться: https://t.me/{username}?start=lead_{lead_id}"
    )
