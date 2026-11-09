"""Safe recipient selection for future targeted lead notifications.

No Telegram messages are sent by this module. A lead must have structured
category, region and area before it can be matched. Free-text leads are not
guessed or broadcast to everyone.
"""
import provider_registry


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


def notification_text(*, lead_id, category, region, area, work_date, details):
    """Only public lead details; never customer phone."""
    if not isinstance(lead_id, int) or lead_id <= 0:
        raise ValueError("Invalid lead id")
    if category not in provider_registry.CATEGORIES or region not in provider_registry.REGIONS:
        raise ValueError("Unrecognized lead category or region")
    if not isinstance(area, str) or not area.strip():
        raise ValueError("Missing lead area")
    text = (
        f"🚜 Подходящая заявка №{lead_id}\n"
        f"Техника: {category}\n"
        f"Регион: {region}, {area.strip()}\n"
        f"Дата: {work_date}"
    )
    return text + (f"\nОписание: {details}" if details else "")
