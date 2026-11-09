import unittest
from types import SimpleNamespace as Record
from unittest.mock import AsyncMock

from render_test_guard import IsolationError, TelegramIsolation, verify_identity


def api(name, **fields):
    return Record(__api_method__=name, **fields)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = TelegramIsolation({101, 102}, 101, -100123)
        self.channel = Record(type="channel", username=None, title="TEST MSK PR6",
                              active_usernames=None, linked_chat_id=None)
        self.members = {
            101: Record(status="creator"),
            102: Record(status="member"),
            900: Record(status="administrator", can_post_messages=True),
        }
        self.bot = Record(
            id=900,
            get_chat=AsyncMock(return_value=self.channel),
            get_chat_member=AsyncMock(side_effect=lambda channel_id, user_id: self.members[user_id]),
            get_chat_member_count=AsyncMock(return_value=3),
        )
        self.request = AsyncMock(return_value="sent")

    async def send(self, method):
        return await self.guard.outgoing(self.request, self.bot, method)

    def update(self, user_id=101, chat_id=101, chat_type="private", callback_id=None):
        user = Record(id=user_id, is_bot=False)
        message = Record(from_user=user, chat=Record(id=chat_id, type=chat_type))
        callback = Record(id=callback_id, from_user=user, message=message) if callback_id else None
        return Record(message=None if callback else message, callback_query=callback)

    def test_token_identity(self):
        me = Record(username="MSK_TEXBot", is_bot=True)
        with self.assertRaises(IsolationError):
            verify_identity(me, "MSK_TEXBot")
        with self.assertRaises(IsolationError):
            verify_identity(me, "msktex_test_demo_bot")
        verify_identity(Record(username="MSKTEX_TEST_Demo_Bot", is_bot=True), "msktex_test_demo_bot")

    async def test_only_paired_private_destinations(self):
        self.assertEqual(await self.send(api("sendMessage", chat_id=101)), "sent")
        self.assertEqual(await self.send(api("editMessageText", chat_id=102)), "sent")
        for target in (999, -100987, "@outside", "101", True):
            with self.subTest(target=target), self.assertRaises(IsolationError):
                await self.send(api("sendMessage", chat_id=target))
        self.assertEqual(self.request.await_count, 2)

    async def test_keyboard_edits_only_for_allowed_private_chat(self):
        self.assertEqual(await self.send(api("editMessageReplyMarkup", chat_id=101)), "sent")
        for chat_id in (999, -100123):
            with self.assertRaises(IsolationError):
                await self.send(api("editMessageReplyMarkup", chat_id=chat_id))
        self.request.assert_awaited_once()

    async def test_contact_only_for_allowed_private_chat(self):
        self.assertEqual(await self.send(api("sendContact", chat_id=101)), "sent")
        for chat_id in (999, -100123):
            with self.assertRaises(IsolationError):
                await self.send(api("sendContact", chat_id=chat_id))
        self.request.assert_awaited_once()

    async def test_public_or_linked_channel_rejected(self):
        for field, value in (("username", "public"), ("active_usernames", ["public"]),
                             ("linked_chat_id", -100987), ("title", "Production"),
                             ("type", "supergroup")):
            before = getattr(self.channel, field)
            setattr(self.channel, field, value)
            with self.subTest(field=field), self.assertRaises(IsolationError):
                await self.send(api("sendMessage", chat_id=-100123))
            setattr(self.channel, field, before)
        self.request.assert_not_awaited()

    async def test_channel_membership_rechecked_before_each_send(self):
        await self.send(api("sendMessage", chat_id=-100123))
        self.bot.get_chat_member_count.return_value = 4
        with self.assertRaises(IsolationError):
            await self.send(api("sendMessage", chat_id=-100123))
        self.request.assert_awaited_once()
        self.assertEqual(self.bot.get_chat_member_count.await_count, 2)

    async def test_channel_owner_and_publish_right_required(self):
        self.members[101].status = "administrator"
        with self.assertRaises(IsolationError):
            await self.guard.check_channel(self.bot)
        self.members[101].status = "creator"
        self.members[900].can_post_messages = False
        with self.assertRaises(IsolationError):
            await self.guard.check_channel(self.bot)
        self.request.assert_not_awaited()

    async def test_unjoined_tester_does_not_count_as_channel_member(self):
        self.members[102].status = "left"
        self.bot.get_chat_member_count.return_value = 2
        await self.guard.check_channel(self.bot)

    async def test_unknown_and_payment_methods_rejected(self):
        for name in ("sendPhoto", "forwardMessage", "sendInvoice", "deleteMessage", "deleteWebhook"):
            with self.subTest(method=name), self.assertRaises(IsolationError):
                await self.send(api(name, chat_id=101))
        self.request.assert_not_awaited()

    async def test_unknown_incoming_ignored(self):
        handler = AsyncMock()
        for update in (
            self.update(999, 999), self.update(101, 102), self.update(101, -100123, "group"),
            self.update(999, 101, callback_id="unpaired"), Record(message=None, callback_query=None),
        ):
            await self.guard.incoming(handler, update, {})
        handler.assert_not_awaited()
        await self.guard.incoming(handler, self.update(), {})
        handler.assert_awaited_once()

    async def test_callback_only_in_matching_context(self):
        async def respond(update, data):
            await self.send(api("answerCallbackQuery", callback_query_id="legitimate"))
            with self.assertRaises(IsolationError):
                await self.send(api("answerCallbackQuery", callback_query_id="different"))

        update = self.update(callback_id="legitimate")
        await self.guard.incoming(respond, update, {})
        with self.assertRaises(IsolationError):
            await self.send(api("answerCallbackQuery", callback_query_id="legitimate"))
        self.request.assert_awaited_once()

    async def test_read_only_preflight(self):
        await self.send(api("getChat"))
        await self.send(api("getUpdates"))
        self.assertEqual(self.request.await_count, 2)


if __name__ == "__main__":
    unittest.main()
