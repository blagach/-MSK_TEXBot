import unittest
from types import SimpleNamespace as R
from unittest.mock import AsyncMock, MagicMock
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChatMember
from telegram_readonly_probe import probe

class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        env={'RENDER_SERVICE_ID':'synthetic-service','TEST_BOT_TOKEN':'synthetic-secret',
             'TEST_ADMIN_ID':'101','TEST_CHANNEL_ID':'-100123','TESTER_IDS':'101,102,103'}
        members={900:R(status='administrator',can_post_messages=True),101:R(status='creator'),102:R(status='member')}
        client=R(get_me=AsyncMock(return_value=R(id=900,is_bot=True,username='example_test_bot')),
                 get_webhook_info=AsyncMock(return_value=R(url='')),
                 get_chat=AsyncMock(return_value=R(type='channel',username=None,title='TEST MSK Example')),
                 get_chat_member=AsyncMock(side_effect=lambda channel,user:members[user]),
                 get_chat_member_count=AsyncMock(return_value=3))
        factory=MagicMock();factory.return_value.__aenter__.return_value=client
        return env,client,factory
    async def test_only_approved_pair_is_queried_and_output_has_no_identifiers(self):
        env,client,factory=self.fixture()
        result=await probe(env,{101,102},'synthetic-service','example_test_bot',factory)
        self.assertEqual(result['result'],'ok')
        self.assertTrue(result['configured_extra_present'])
        self.assertEqual([x.args[1] for x in client.get_chat_member.await_args_list],[900,101,102])
        self.assertEqual(len(result['checks']),7)
        for secret in ('synthetic-secret','-100123','101','102','103','example_test_bot'):
            self.assertNotIn(secret,str(result))
    async def test_api_failure_is_sanitized_and_no_third_actor_is_contacted(self):
        env,client,factory=self.fixture()
        client.get_chat_member.side_effect=TelegramBadRequest(GetChatMember(chat_id=-100123,user_id=101),
                                                             'user not found: synthetic-secret')
        result=await probe(env,{101,102},'synthetic-service','example_test_bot',factory)
        self.assertEqual(result['result'],'STOP')
        self.assertNotIn('synthetic-secret',str(result))
        self.assertIn('USER_NOT_FOUND',str(result))
        self.assertNotIn(103,[x.args[1] for x in client.get_chat_member.await_args_list])
    async def test_wrong_context_stops_before_telegram_access(self):
        env,client,factory=self.fixture()
        with self.assertRaises(ValueError):
            await probe(env,{101,102},'wrong-service','example_test_bot',factory)
        factory.assert_not_called()
    async def test_unknown_channel_member_stays_blocked(self):
        env,client,factory=self.fixture();client.get_chat_member_count.return_value=4
        result=await probe(env,{101,102},'synthetic-service','example_test_bot',factory)
        self.assertEqual(result['result'],'STOP')
