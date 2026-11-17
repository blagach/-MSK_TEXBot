"""User-confirmed invitations; never send to a selected recipient ourselves."""
from urllib.parse import urlencode

from aiogram.types import (CopyTextButton, InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup,
                           InlineQueryResultArticle, InputTextMessageContent, SwitchInlineQueryChosenChat)
import referral_storage as referrals
import storage
import aiosqlite

LABEL = 'Поделиться ботом'
DESCRIPTION = ('MSK TEXBot — заявки на спецтехнику и поиск исполнителей в Москве и Московской области. '
               'Заявки и отклики — внутри бота.')


def entry_markup():
    return Markup(inline_keyboard=[[Button(text=LABEL, callback_data='ref:open')]])


def stats_markup():
    return Markup(inline_keyboard=[[Button(text='Назад', callback_data='ref:open')],
                                  [Button(text='Главное меню', callback_data='ref:home')]])


def url(username, code):
    return f'https://t.me/{username}?start=r1_{code}'


def invitation_markup(link, inline):
    send = (Button(text='Отправить приглашение', switch_inline_query_chosen_chat=SwitchInlineQueryChosenChat(
        query='invite', allow_user_chats=True, allow_bot_chats=False, allow_group_chats=True, allow_channel_chats=False))
            if inline else Button(text='Отправить приглашение', url='https://t.me/share/url?'+urlencode({'url': link, 'text': DESCRIPTION})))
    rows = [[send]]
    if inline:
        rows.append([Button(text='Отправить через Telegram', url='https://t.me/share/url?'+urlencode({'url': link, 'text': DESCRIPTION}))])
    rows.extend([[Button(text='Копировать ссылку', copy_text=CopyTextButton(text=link))],
                 [Button(text='Моя статистика', callback_data='ref:stats')],
                 [Button(text='Назад', callback_data='ref:back')],
                 [Button(text='Главное меню', callback_data='ref:home')]])
    return Markup(inline_keyboard=rows)


def summary(s):
    return (f"Новых пользователей пришло: {s['arrived']}\nНачали пользоваться сервисом: {s['active']}\n"
            f"Первая активная заявка: {s['customers']}\nЗавершили регистрацию исполнителя: {s['providers']}\n\n"
            'Один человек может выполнить оба действия. Переходы без запуска бота не учитываются.')


async def available(uid):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        return (await referrals.mode_in(db))['enabled'] and await referrals.eligible_in(db, uid)


async def show_entry(message, uid):
    try:
        enabled = await available(uid)
    except Exception as error:
        referrals.fault('menu_availability', error)
        return
    if enabled:
        await message.answer('Пригласите тех, кому нужна спецтехника или кто выполняет работы.', reply_markup=entry_markup())


async def show(message, uid, bot, username):
    code = await referrals.link_code(uid)
    if code is None:
        await message.answer('Приглашения сейчас недоступны.')
        return
    identity = await bot.me()  # read-only capability; Bot caches getMe. Never enable inline here.
    link = url(username, code)
    inline = identity.supports_inline_queries is True
    await message.answer(
        f'{LABEL}\n\n{DESCRIPTION}\n\nВаша ссылка:\n{link}\n\n'
        'Получателя выбираете вы. Отправка произойдёт только после вашего действия. '
        + ('После выбора чата нажмите карточку приглашения — это сразу отправит её. '
           'Если inline не открылся, используйте «Отправить через Telegram». ' if inline else '')
        + 'Мы учитываем приглашение при первом запуске новым пользователем; пригласивший видит только общие числа.',
        reply_markup=invitation_markup(link, inline))


async def inline(query, username):
    # Ignore any supplied referral code: the sender always gets their OWN link.
    if (query.from_user.is_bot or query.query.strip() != 'invite'
            or getattr(query, 'chat_type', None) not in {None, 'sender', 'private', 'group', 'supergroup'}):
        await query.answer([], cache_time=0, is_personal=True)
        return
    code = await referrals.link_code(query.from_user.id)
    results = []
    if code is not None:
        results = [InlineQueryResultArticle(id='referral-invitation', title='Отправить приглашение MSK TEXBot',
                   description='Нажмите карточку, чтобы отправить приглашение в выбранный чат.',
                   input_message_content=InputTextMessageContent(message_text=DESCRIPTION+'\n\n'+url(username, code)))]
    await query.answer(results, cache_time=0, is_personal=True)
