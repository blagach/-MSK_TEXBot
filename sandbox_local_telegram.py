"""An aiogram adapter with a non-network session; never a Telegram sender.

There is no polling entrypoint and no token/config import. Used only by local
dispatcher acceptance tests with synthetic updates and owned sandbox SQLite.
"""
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message


class LocalPreviewSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.sequence = 100

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        if bot.id != 99:
            raise RuntimeError('Only the synthetic local bot is permitted')
        self.calls.append(method)
        name = method.__api_method__
        if name in ('editMessageReplyMarkup', 'answerCallbackQuery'):
            return True
        if name in ('sendMessage', 'sendPhoto', 'sendVideo', 'sendDocument'):
            self.sequence += 1
            return Message(message_id=self.sequence, date=1, chat={'id': method.chat_id, 'type': 'private'},
                           text=getattr(method, 'text', None))
        raise RuntimeError('Network/polling/file download operations are disabled')

    async def stream_content(self, *args, **kwargs):
        raise RuntimeError('File downloads are disabled')
        yield b''  # Async iterator contract; never reached.


def local_bot():
    return Bot('99:LOCAL_PREVIEW_ONLY', session=LocalPreviewSession())


def local_dispatcher(admin, clock):
    """Build standalone local routes; never add these to the deployed bot."""
    router = Router(name='sandbox_local_admin')
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    def actor(message, bot):
        if type(bot.session) is not LocalPreviewSession or bot.id != 99:
            raise RuntimeError('A real Telegram session is forbidden')
        if (message.chat.type != 'private' or not message.from_user or message.from_user.is_bot
                or message.chat.id != message.from_user.id):
            raise PermissionError('Only a private synthetic conversation is permitted')
        return message.from_user.id

    async def post(message, panel, uid, old_id):
        if old_id:
            try:
                await message.bot.edit_message_reply_markup(chat_id=uid, message_id=old_id, reply_markup=None)
            except TelegramBadRequest:
                pass  # Durable screen nonces still reject the old buttons.
        if panel.preview_text:
            await message.answer(panel.preview_text, parse_mode=None)
        for item in panel.attachments:
            await getattr(message.bot, 'send_' + item['kind'])(chat_id=uid, **{item['kind']: item['token']})
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data)] for label, data in panel.buttons])
        sent = await message.answer(panel.text, reply_markup=markup, parse_mode=None)
        admin.set_prompt(uid, admin.session(uid)['nonce'], sent.message_id)

    @router.message(CommandStart())
    async def start(message: Message, bot):
        uid = actor(message, bot)
        old = admin.session(uid)
        try:
            panel = admin.start(uid)
        except PermissionError as error:
            await message.answer(str(error))
            return
        await post(message, panel, uid, old['prompt_id'] if old else None)

    async def post_consent(message, uid):
        with admin.store.connection() as db:
            old = db.execute('SELECT panel_id FROM sandbox_consent_panels WHERE recipient=?', (uid,)).fetchone()
        panel = admin.consent_panel(uid)
        if old and old[0]:
            try:
                await message.bot.edit_message_reply_markup(chat_id=uid, message_id=old[0], reply_markup=None)
            except TelegramBadRequest:
                pass
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data)] for label, data in panel.buttons])
        sent = await message.answer(panel.text, reply_markup=markup)
        nonce = panel.buttons[0][1].split(':')[1]
        with admin.store.transaction() as db:
            db.execute('UPDATE sandbox_consent_panels SET panel_id=? WHERE recipient=? AND nonce=?',
                       (sent.message_id, uid, nonce))

    @router.message(Command('consent'))
    async def consent(message: Message, bot):
        uid = actor(message, bot)
        try:
            await post_consent(message, uid)
        except ValueError as error:
            await message.answer(str(error))

    @router.callback_query(F.data.startswith('sc:'))
    async def consent_click(callback, bot):
        if type(bot.session) is not LocalPreviewSession or bot.id != 99:
            raise RuntimeError('A real Telegram session is forbidden')
        uid = callback.from_user.id
        if not callback.message or callback.message.chat.type != 'private' or callback.message.chat.id != uid:
            await callback.answer('Нет доступа.')
            return
        try:
            _, nonce, action = callback.data.split(':')
            if action not in ('on', 'off'):
                raise ValueError('Неизвестный ответ.')
            admin.consent_response(uid, nonce, action == 'on', clock())
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer()
        await post_consent(callback.message, uid)

    @router.callback_query(F.data.startswith('sb:'))
    async def click(callback, bot):
        # Callback messages are authored by the bot; authorization always uses
        # callback.from_user rather than message.from_user.
        if type(bot.session) is not LocalPreviewSession or bot.id != 99:
            raise RuntimeError('A real Telegram session is forbidden')
        uid = callback.from_user.id
        if not callback.message or callback.message.chat.type != 'private' or callback.message.chat.id != uid:
            await callback.answer('Нет доступа.')
            return
        old = admin.session(uid)
        try:
            panel = admin.handle(uid, callback.data, clock())
        except (ValueError, PermissionError) as error:
            await callback.answer(str(error), show_alert=True)
            current = admin.session(uid)
            # A rejected input/confirmation rotates the nonce. Repaint that
            # current screen so navigation does not retain only stale buttons.
            if old and current and current['nonce'] != old['nonce'] and not current['busy']:
                await post(callback.message, admin.render(uid), uid, old['prompt_id'])
            return
        await callback.answer()
        await post(callback.message, panel, uid, old['prompt_id'] if old else None)

    @router.message()
    async def input_message(message: Message, bot):
        uid = actor(message, bot)
        old = admin.session(uid)
        if not old or not message.reply_to_message or message.reply_to_message.message_id != old['prompt_id']:
            await message.answer('Ответьте на последнее сообщение текущей формы.')
            return
        attachment = None
        if message.photo:
            attachment = {'kind': 'photo', 'token': message.photo[-1].file_id}
        elif message.video:
            attachment = {'kind': 'video', 'token': message.video.file_id}
        elif message.document:
            attachment = {'kind': 'document', 'token': message.document.file_id}
        try:
            panel = admin.text(uid, old['nonce'], message.text or message.caption or '', clock(), attachment)
        except (ValueError, PermissionError) as error:
            await message.answer(str(error))
            if isinstance(error, PermissionError):
                return
            panel = admin.render(uid)
        await post(message, panel, uid, old['prompt_id'])

    return dispatcher
