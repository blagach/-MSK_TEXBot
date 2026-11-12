"""Synthetic actors only: test startup isolation separately from recipient delivery."""
import os
import unittest
from types import SimpleNamespace as Record
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import GetChatMember, SendMessage
from render_test_guard import TelegramIsolation
from startup_diagnostics import startup_failure_message

class TwoAccountDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_optional_actor_reproduces_startup_failure_but_pair_passes(self):
        members={101:Record(status='creator'),102:Record(status='member'),900:Record(status='administrator',can_post_messages=True)}
        async def member(channel,user):
            if user not in members:
                raise TelegramBadRequest(GetChatMember(chat_id=channel,user_id=user),'user not found')
            return members[user]
        api=Record(id=900,get_chat=AsyncMock(return_value=Record(type='channel',username=None,active_usernames=None,linked_chat_id=None,title='TEST MSK Example')),
                   get_chat_member=AsyncMock(side_effect=member),get_chat_member_count=AsyncMock(return_value=3))
        configured={101,102,103}
        with self.assertRaises(TelegramBadRequest):
            await TelegramIsolation(configured,101,-100123).check_channel(api)
        self.assertEqual(configured,{101,102,103})
        await TelegramIsolation({101,102},101,-100123).check_channel(api)

    async def test_failed_recipient_does_not_stop_next_notification(self):
        os.environ.setdefault('BOT_TOKEN','123456:TEST')
        os.environ.setdefault('ADMIN_IDS','101')
        os.environ.setdefault('LEADS_CHANNEL_ID','-100123')
        import bot
        for error_class in (TelegramBadRequest,TelegramForbiddenError):
            with self.subTest(error=error_class.__name__):
                api=Record(send_message=AsyncMock(side_effect=[error_class(SendMessage(chat_id=101,text='test'),'unavailable'),Record(message_id=1)]))
                outcome=AsyncMock()
                with (patch.object(bot,'settings',Record(test_notification_provider_ids={101,102},bot_username='example_test_bot')),
                      patch.object(bot.lead_routing,'get',AsyncMock(return_value={'category':'каток','region':'Москва','area':'СЗАО'})),
                      patch.object(bot.provider_dispatch,'recipients_for_lead',AsyncMock(return_value=[101,102])),
                      patch.object(bot.notification_reservations,'reserve',AsyncMock(return_value=True)),
                      patch.object(bot.notification_reservations,'record_outcome',outcome)):
                    await bot.notify_test_providers(api,{'id':70,'work_date':'future','details':'test'})
                self.assertEqual([call.args[0] for call in api.send_message.await_args_list],[101,102])
                self.assertEqual([call.args[2] for call in outcome.await_args_list],['uncertain','confirmed'])

    def test_safe_diagnostics_do_not_include_payload_or_unknown_method(self):
        error=TelegramBadRequest(GetChatMember(chat_id=-100123,user_id=103),'private request contents')
        self.assertEqual(startup_failure_message(error),'Test startup failed: TelegramBadRequest [getChatMember]')
        error.method=Record(__api_method__='private request contents')
        self.assertEqual(startup_failure_message(error),'Test startup failed: TelegramBadRequest')
