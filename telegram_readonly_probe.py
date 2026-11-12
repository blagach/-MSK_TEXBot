"""Bounded Telegram startup probe: no Worker, SQLite, polling or send operations."""
import argparse
import asyncio
import json
import os

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError


async def probe(env, approved, service_id, bot_username, factory=None):
    approved = frozenset(approved)
    if len(approved) != 2 or any(type(x) is not int or x <= 0 for x in approved):
        raise ValueError('Exactly two approved actors required')
    if env.get('RENDER_SERVICE_ID') != service_id:
        raise ValueError('Unexpected service context')
    token = env.get('TEST_BOT_TOKEN', '')
    channel = int(env.get('TEST_CHANNEL_ID', '0'))
    admin = int(env.get('TEST_ADMIN_ID', '0'))
    if not token or admin not in approved or channel >= 0:
        raise ValueError('Invalid test configuration')
    configured = frozenset(int(x.strip()) for x in env.get('TESTER_IDS', '').split(',') if x.strip())
    report = {'result': 'STOP', 'checks': [],
              'configured_extra_present': bool(configured - approved),
              'configuration_matches_approved': configured == approved,
              'worker_started': False, 'polling_started': False, 'messages_sent': 0}
    async def read(api, actor, call):
        try:
            value = await asyncio.wait_for(call(), timeout=6)
        except TelegramAPIError as error:
            reason = str(getattr(error, 'message', '')).lower()
            code = next((name for text, name in (
                ('user not found', 'USER_NOT_FOUND'), ('chat not found', 'CHAT_NOT_FOUND'),
                ('not enough rights', 'INSUFFICIENT_RIGHTS'),
            ) if text in reason), 'TELEGRAM_API_ERROR')
            report['checks'].append({'api': api, 'actor': actor, 'ok': False, 'error': code})
            return None
        report['checks'].append({'api': api, 'actor': actor, 'ok': True})
        return value
    factory = factory or (lambda value: Bot(value, session=AiohttpSession(timeout=6)))
    async with factory(token) as client:
        me = await read('getMe', 'bot', client.get_me)
        if me is None or not me.is_bot or (me.username or '').lower() != bot_username.lower():
            return report
        webhook = await read('getWebhookInfo', 'bot', client.get_webhook_info)
        chat = await read('getChat', 'channel', lambda: client.get_chat(channel))
        member = await read('getChatMember', 'bot', lambda: client.get_chat_member(channel, me.id))
        valid = (webhook is not None and not webhook.url and chat is not None
                 and chat.type == 'channel' and not chat.username
                 and not getattr(chat, 'active_usernames', None)
                 and not getattr(chat, 'linked_chat_id', None)
                 and (chat.title or '').startswith('TEST MSK')
                 and member is not None and member.status == 'administrator'
                 and member.can_post_messages)
        expected_count = 1
        for user in sorted(approved):
            role = 'test_admin' if user == admin else 'test_peer'
            member = await read('getChatMember', role, lambda user=user: client.get_chat_member(channel, user))
            if member is None:
                valid = False
                continue
            if user == admin and member.status != 'creator':
                valid = False
            expected_count += member.status in {'creator', 'administrator', 'member'} or (
                member.status == 'restricted' and getattr(member, 'is_member', False))
        actual_count = await read('getChatMemberCount', 'channel', lambda: client.get_chat_member_count(channel))
        valid = valid and actual_count == expected_count
        report['pair_channel_checks'] = bool(valid)
        if valid and configured == approved:
            report['result'] = 'ok'
        elif valid:
            report['reason'] = 'APPROVED_SET_MISMATCH'
        else:
            report['reason'] = 'PAIR_CHECK_FAILED'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service-id', required=True)
    parser.add_argument('--bot-username', required=True)
    parser.add_argument('--tester-ids', required=True)
    args = parser.parse_args()
    try:
        actors = [int(x) for x in args.tester_ids.split(',')]
        result = asyncio.run(asyncio.wait_for(probe(os.environ, actors, args.service_id,
                                                   args.bot_username), timeout=45))
    except Exception:
        result = {'result': 'STOP', 'reason': 'Probe failed or timed out; no exception payload logged'}
    print(json.dumps(result))
    raise SystemExit(0 if result['result'] == 'ok' else 1)


if __name__ == '__main__':
    main()
