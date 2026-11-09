"""Fail-closed Telegram boundary for the Render test worker."""
from contextvars import ContextVar


class IsolationError(RuntimeError):
    pass


def verify_identity(me, expected_username):
    actual = (me.username or "").lower()
    if not me.is_bot or actual != expected_username.lower() or actual == "msk_texbot":
        raise IsolationError("Токен не принадлежит указанному отдельному тестовому боту.")


def belongs_to_channel(member):
    return member.status in {"creator", "administrator", "member"} or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )


class TelegramIsolation:
    read_methods = frozenset({
        "getMe", "getWebhookInfo", "getUpdates",
        "getChat", "getChatMember", "getChatMemberCount",
    })

    def __init__(self, testers, admin_id, channel_id):
        self.testers = frozenset(testers)
        self.admin_id = admin_id
        self.channel_id = channel_id
        if admin_id not in self.testers or any(
            type(user_id) is not int or user_id <= 0 for user_id in self.testers
        ):
            raise IsolationError("Неверный список тестовых участников.")
        if type(channel_id) is not int or not str(channel_id).startswith("-100"):
            raise IsolationError("Нужен числовой ID закрытого тестового канала.")
        self.callback_id = ContextVar("test_callback_id", default=None)

    async def check_channel(self, bot):
        chat = await bot.get_chat(self.channel_id)
        if (chat.type != "channel" or chat.username or getattr(chat, "active_usernames", None)
                or getattr(chat, "linked_chat_id", None)
                or not (chat.title or "").startswith("TEST MSK")):
            raise IsolationError(
                "Нужен закрытый канал с названием, начинающимся на TEST MSK, без публичной ссылки и чата обсуждений."
            )
        bot_member = await bot.get_chat_member(self.channel_id, bot.id)
        if bot_member.status != "administrator" or not bot_member.can_post_messages:
            raise IsolationError("Выдайте тестовому боту право публикации в закрытом канале.")
        approved_count = 1
        for tester_id in self.testers:
            member = await bot.get_chat_member(self.channel_id, tester_id)
            if tester_id == self.admin_id and member.status != "creator":
                raise IsolationError("Первый подключённый тестировщик должен быть владельцем тестового канала.")
            approved_count += belongs_to_channel(member)
        if await bot.get_chat_member_count(self.channel_id) != approved_count:
            raise IsolationError("В канале есть неподтверждённые участники. Публикация заблокирована.")

    async def outgoing(self, make_request, bot, method):
        api = method.__api_method__
        if api in self.read_methods:
            return await make_request(bot, method)
        if api in {"sendMessage", "editMessageText", "sendContact"}:
            destination = method.chat_id
            if type(destination) is int and destination in self.testers:
                return await make_request(bot, method)
            if api == "sendMessage" and type(destination) is int and destination == self.channel_id:
                await self.check_channel(bot)
                return await make_request(bot, method)
        if api == "answerCallbackQuery" and method.callback_query_id == self.callback_id.get():
            return await make_request(bot, method)
        raise IsolationError("Тестовый режим заблокировал исходящий запрос Telegram.")

    async def incoming(self, handler, update, data):
        message = update.message
        callback = update.callback_query
        if message is None and callback is None:
            return None
        actor = message.from_user if message is not None else callback.from_user
        source = message if message is not None else callback.message
        if (actor is None or actor.is_bot or actor.id not in self.testers or source is None
                or source.chat.type != "private" or source.chat.id != actor.id):
            return None
        marker = self.callback_id.set(callback.id if callback is not None else None)
        try:
            return await handler(update, data)
        finally:
            self.callback_id.reset(marker)
