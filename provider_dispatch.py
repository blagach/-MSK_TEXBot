"""Safe recipient selection for future targeted lead notifications.

No Telegram messages are sent by this module. A lead must have structured
category, region and area before it can be matched. Free-text leads are not
guessed or broadcast to everyone.
"""
import asyncio
from dataclasses import dataclass, field

import provider_registry


@dataclass(frozen=True)
class ProductionNotificationScope:
    bot_id: int
    channel_id: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, compare=False)


async def verify_production_scope(telegram, username, channel_id, *, restricted_ids=(), test_configured=False):
    """Authorize only the working bot/channel; a missing scope never enables sends."""
    if restricted_ids or test_configured or username.casefold() != "msk_texbot":
        raise RuntimeError("Production notification identity conflicts with test configuration")
    identity = await telegram.get_me()
    if (identity.is_bot is not True or (identity.username or "").casefold() != "msk_texbot"
            or type(identity.id) is not int or identity.id != telegram.id):
        raise RuntimeError("Unexpected production bot identity")
    channel = await telegram.get_chat(channel_id)
    if (channel.id != channel_id or channel.type != "channel"
            or (channel.username or "").casefold() != "zayavki_specteh_msk"):
        raise RuntimeError("Unexpected production notification channel")
    member = await telegram.get_chat_member(channel_id, identity.id)
    if member.status != "creator" and not (
        member.status == "administrator" and getattr(member, "can_post_messages", False) is True
    ):
        raise RuntimeError("Production bot cannot publish in its channel")
    return ProductionNotificationScope(identity.id, channel_id)


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
