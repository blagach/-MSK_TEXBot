"""Restrict optional support-group notices to a verified admin-only group."""


async def is_private_admin_group(bot, group_id, admin_ids, *, test=False):
    chat = await bot.get_chat(group_id)
    if (
        chat.type not in {"group", "supergroup"} or chat.username
        or getattr(chat, "active_usernames", None) or getattr(chat, "linked_chat_id", None)
        or (test and not (chat.title or "").startswith("TEST MSK"))
    ):
        return False
    member = await bot.get_chat_member(group_id, bot.id)
    if member.status != "administrator":
        return False
    allowed_members = 1
    for admin_id in admin_ids:
        member = await bot.get_chat_member(group_id, admin_id)
        if member.status in {"creator", "administrator", "member"}:
            allowed_members += 1
    return allowed_members > 1 and await bot.get_chat_member_count(group_id) == allowed_members
