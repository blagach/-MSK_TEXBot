"""Local checkout routes, guarded by the same network-free Telegram session."""
import json
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def checkout_router(checkout, clock, *, session_guard=None):
    from sandbox_local_telegram import LocalPreviewSession
    router = Router(name='sandbox_checkout')
    store = checkout.store
    store.schema('''CREATE TABLE IF NOT EXISTS sandbox_checkout_ui (
        payer INTEGER PRIMARY KEY, checkout_id TEXT, nonce TEXT NOT NULL,
        busy INTEGER NOT NULL DEFAULT 0, panel_id INTEGER
    );''')

    def guard(bot, uid, chat):
        if session_guard is not None:
            session_guard(bot, uid, chat)
        elif type(bot.session) is not LocalPreviewSession or bot.id != 99:
            raise RuntimeError('Only the network-free local Telegram session is permitted')
        if chat.type != 'private' or chat.id != uid:
            raise PermissionError('Откройте локальное оформление в личном чате.')

    def session(uid):
        with store.connection() as db:
            row = db.execute('SELECT * FROM sandbox_checkout_ui WHERE payer=?', (uid,)).fetchone()
            return dict(row) if row else None

    async def show(message, uid):
        s = session(uid)
        nonce = s['nonce']
        def button(label, action, value=''):
            data = f'co:{nonce}:{action}:{value}'
            if len(data.encode()) > 64:
                raise ValueError('Use short local catalogue identifiers')
            return [InlineKeyboardButton(text=label, callback_data=data)]
        rows = []
        text = 'Локальный checkout. Только условные TEST; настоящей оплаты нет.\n'
        text += 'Режим прав: '+('проверка TEST-квот.' if checkout.enforce_paid else 'бесплатный, ограничения не активированы.')
        if s['busy']:
            text += '\nОперация прервана. Сначала обновите статус; автоматического повтора нет.'
            rows.append(button('Обновить статус', 'status'))
        elif s['checkout_id']:
            c = checkout.owned(uid, s['checkout_id'])
            terms = json.loads(c['terms'])
            model = {'function': 'разовая функция', 'package': 'пакет прав', 'period': 'доступ на срок'}[terms['model']]
            text += f"\n{terms['title']}\nМодель: {model}; сумма: {terms['amount']} TEST.\n"
            text += ('Без расхода квоты. ' if terms['model'] == 'period' else f"Квота: {terms['units']}. ")
            text += (f"Срок от подтверждения: {terms['duration']} сек.\n" if terms['duration'] else 'Без ограничения срока.\n')
            if c['state'] == 'review':
                text += 'Проверьте условия и подтвердите оформление.'
                rows.extend((button('Подтвердить оформление', 'submit'), button('Отменить оформление', 'cancel')))
            else:
                order = checkout.payments.get_order(uid, c['order_id']) if c['order_id'] else None
                status = order['status'] if order else 'cancelled'
                text += 'Статус: '+status
                if status in ('pending', 'failed'):
                    rows.extend((button('Подставной провайдер: оплатить', 'paid'),
                                 button('Подставная ошибка', 'failed'), button('Отменить заказ', 'cancel')))
                elif status == 'paid':
                    rows.append(button('Подставной полный возврат', 'refunded'))
            rows.append(button('К учебным предложениям', 'catalog'))
        else:
            for offer in list(checkout.offers.values())[:8]:
                rows.append(button(f'{offer.title} · {offer.amount} TEST', 'offer', offer.id))
        if not s['busy']:
            for resource in list(checkout.resources.values())[:8]:
                rows.append(button('Открыть '+resource.id, 'resource', resource.id))
        if s['panel_id']:
            try:
                await message.bot.edit_message_reply_markup(chat_id=uid, message_id=s['panel_id'], reply_markup=None)
            except TelegramBadRequest:
                pass
        sent = await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        with store.transaction() as db:
            db.execute('UPDATE sandbox_checkout_ui SET panel_id=? WHERE payer=? AND nonce=?', (sent.message_id, uid, nonce))

    @router.message(Command('checkout'))
    async def start(message, bot):
        uid = message.from_user.id
        guard(bot, uid, message.chat)
        with store.transaction() as db:
            old = db.execute('SELECT * FROM sandbox_checkout_ui WHERE payer=?', (uid,)).fetchone()
            if not old:
                db.execute('INSERT INTO sandbox_checkout_ui VALUES (?,NULL,?,0,NULL)', (uid, uuid4().hex[:12]))
            elif not old['busy']:
                db.execute('UPDATE sandbox_checkout_ui SET nonce=? WHERE payer=?', (uuid4().hex[:12], uid))
        await show(message, uid)

    @router.callback_query(F.data.startswith('co:'))
    async def click(callback, bot):
        uid = callback.from_user.id
        if session_guard is not None:
            session_guard(bot, uid, callback.message.chat if callback.message else None)
        elif type(bot.session) is not LocalPreviewSession or bot.id != 99:
            raise RuntimeError('Only the network-free local Telegram session is permitted')
        if not callback.message:
            await callback.answer('Нет доступного сообщения.')
            return
        guard(bot, uid, callback.message.chat)
        try:
            _, nonce, action, value = callback.data.split(':', 3)
        except ValueError:
            await callback.answer('Некорректная кнопка.')
            return
        s = session(uid)
        if not s or s['nonce'] != nonce or (s['busy'] and action != 'status'):
            await callback.answer('Кнопка устарела; откройте /checkout.', show_alert=True)
            return
        # Match screen and object as well as nonce. Hidden fake confirmation is
        # never accepted for another checkout, customer or screen.
        allowed = {'status'} if s['busy'] else {'resource', 'catalog'}
        c = checkout.owned(uid, s['checkout_id']) if s['checkout_id'] else None
        if not s['busy']:
            if c:
                allowed |= {'submit', 'cancel'} if c['state'] == 'review' else {'paid', 'failed', 'refunded', 'cancel'}
            else:
                allowed.add('offer')
        if action not in allowed or (action == 'offer' and value not in checkout.offers) or (action == 'resource' and value not in checkout.resources):
            await callback.answer('Действие не разрешено.')
            return
        with store.transaction() as db:
            changed = db.execute('UPDATE sandbox_checkout_ui SET busy=1 WHERE payer=? AND nonce=? AND busy=?',
                                 (uid, nonce, int(s['busy']))).rowcount
        if not changed:
            await callback.answer('Действие уже выполняется.')
            return
        cid = s['checkout_id']
        try:
            if action == 'catalog':
                cid = None
            elif action == 'offer':
                cid = (await checkout.begin(uid, value, 'ui:'+nonce))['id']
            elif action == 'submit':
                await checkout.submit(uid, c['id'], c['token'])
            elif action == 'cancel':
                await checkout.cancel(uid, c['id'])
            elif action in ('paid', 'failed', 'refunded'):
                await checkout.simulate(uid, c['id'], action, clock())
            elif action == 'resource':
                result = await checkout.open_resource(uid, value, clock())
                await callback.answer('Нет разрешённого права.' if result is None else
                                      'Ресурс разрешён: повтор без расхода.' if not result[1] else 'Ресурс разрешён, квота учтена.',
                                      show_alert=True)
            # status only reads the persistent checkout/order on redraw. It
            # never retries order creation or a fake payment event.
        except (ValueError, PermissionError) as error:
            await callback.answer(str(error), show_alert=True)
        else:
            if action != 'resource':
                await callback.answer()
        # Unexpected errors deliberately keep busy; /checkout offers only an
        # explicit read-only status refresh after restart.
        with store.transaction() as db:
            db.execute('UPDATE sandbox_checkout_ui SET checkout_id=?,nonce=?,busy=0 WHERE payer=? AND nonce=?',
                       (cid, uuid4().hex[:12], uid, nonce))
        await show(callback.message, uid)

    return router
