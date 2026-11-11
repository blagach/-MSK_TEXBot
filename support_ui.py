"""Private help, training and support workflows."""
import asyncio
import logging
from contextlib import suppress
import re
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from render_test_guard import IsolationError

import support_storage as store
import admin_access
from support_content import FAQ, GUIDES, TRAINING
from support_group import is_private_admin_group

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
HELP_LABEL = "❓ Помощь / Поддержка"
KINDS = {"help": "Вопрос", "bug": "Ошибка"}
STATUSES = {"new": "Новое", "in_progress": "В работе", "closed": "Закрыто"}
ASSETS = Path(__file__).resolve().parent / "assets" / "help"
TICKET_PAGE = 8
HISTORY_PAGE = 6
DELIVERY = {
    "queued": "уведомление в очереди",
    "sending": "уведомление отправляется",
    "sent": "Telegram подтвердил отправку",
    "failed": "уведомление не отправлено",
    "uncertain": "отправка уведомления не подтверждена",
}


class Support(StatesGroup):
    search = State()
    compose = State()
    reply = State()
    requester_reply = State()


def keys(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=f"help:{action}") for text, action in row]
        for row in rows
    ])


BACK = (("⬅️ В помощь", "home"),)


async def menu(message, user_id, admin_ids):
    await message.answer("Помощь / Поддержка", reply_markup=keys(
        (("📖 Как пользоваться сервисом", "resources"),),
        (("📋 Мои обращения", "mine:0"),),
        (("💬 Написать в поддержку", "new:help"),),
    ))


async def faq_list(message, query=None):
    words = re.findall(r"\w+", (query or "").casefold().replace("ё", "е"))
    if query is not None and not words:
        await message.answer("Введите слова для поиска.", reply_markup=keys(BACK))
        return
    matches = []
    for index, (title, answer, tags) in enumerate(FAQ):
        haystack = " ".join((title, answer, tags)).casefold().replace("ё", "е")
        if all(word in haystack for word in words):
            matches.append(((title, f"answer:{index}"),))
    await message.answer(
        "Частые вопросы" if matches else "Ничего не найдено. Попробуйте другие слова.",
        reply_markup=keys(*matches, (("🔎 Поиск", "search"),), BACK),
    )


async def show_guide(message, role, index):
    steps = GUIDES[role]
    title, body = steps[index]
    rows = []
    if index:
        rows.append((("⬅️ Назад", f"guide:{role}:{index - 1}"),))
    if index + 1 < len(steps):
        rows.append((("Далее ➡️", f"guide:{role}:{index + 1}"),))
    else:
        rows.append((("Завершить инструкцию", "home"),))
    rows.append(BACK)
    text = f"{index + 1}/{len(steps)} · {title}\n\n{body}"
    screenshot = ASSETS / f"{role}-{index + 1}.png"
    if screenshot.is_file():
        await message.answer_photo(FSInputFile(screenshot), caption=text, reply_markup=keys(*rows))
    else:
        await message.answer(text, reply_markup=keys(*rows))


async def show_training(message, role, index):
    steps = TRAINING[role]
    if index == len(steps):
        await message.answer("🎉 Тренировка завершена. Учебные данные не отправлялись.", reply_markup=keys(BACK))
        return
    title, button, details = steps[index]
    await message.answer(
        f"🎓 Тренировка · {index + 1}/{len(steps)}\n\n{title}\n{details}",
        reply_markup=keys(((button, f"try:{role}:{index}:1"),), BACK),
    )

async def ticket_list(message, user_id, *, status=None, offset=0):
    rows = await store.list_tickets(requester_id=user_id, status=status, offset=offset)
    prefix = "mine" if user_id is not None else f"list:{status}"
    actions = [
        ((f"№{row['id']} · {KINDS[row['kind']]} · {STATUSES[row['status']]}", f"ticket:{row['id']}:0:{prefix}:{offset}"),)
        for row in rows[:TICKET_PAGE]
    ]
    if offset:
        actions.append((("⬅️ Предыдущие", f"{prefix}:{offset - TICKET_PAGE}"),))
    if len(rows) > TICKET_PAGE:
        actions.append((("Следующие ➡️", f"{prefix}:{offset + TICKET_PAGE}"),))
    if user_id is None:
        actions.append((("⬅️ К статусам", "queue"),))
    await message.answer("Обращения" if rows else "Обращений пока нет.", reply_markup=keys(*actions, BACK))


def ticket_list_origin(parts, admin):
    if len(parts) == 2 and parts[0] == "mine" and page_number(parts[1], TICKET_PAGE) is not None:
        return ":".join(parts)
    if admin and len(parts) == 3 and parts[0] == "list" and parts[1] in STATUSES:
        if page_number(parts[2], TICKET_PAGE) is not None:
            return ":".join(parts)
    return ""


async def show_ticket(message, ticket_id, user_id, admin, offset=0, origin=""):
    suffix = f":{origin}" if origin else ""
    ticket = await store.ticket(ticket_id, None if admin else user_id)
    if ticket is None:
        await message.answer("Обращение недоступно.", reply_markup=keys(BACK))
        return
    rows = await store.history(ticket_id, offset)
    for row in reversed(rows[:HISTORY_PAGE]):
        who = "Поддержка" if row["is_admin"] else "Автор обращения"
        delivery = ""
        if admin and row["is_admin"]:
            status = DELIVERY.get(row["delivery_status"], "нет записи о доставке")
            error = f" ({row['delivery_error']})" if row["delivery_error"] else ""
            delivery = f"\nСтатус уведомления: {status}{error}."
        await message.answer(f"№{ticket_id} · {who}\n{row['text']}{delivery}")
        if row["photo_id"]:
            try:
                await message.answer_photo(row["photo_id"])
            except TelegramAPIError:
                await message.answer("Скриншот сейчас недоступен.")
    actions = []
    if offset:
        actions.append((("К новым сообщениям", f"ticket:{ticket_id}:{offset - HISTORY_PAGE}{suffix}"),))
    if len(rows) > HISTORY_PAGE:
        actions.append((("Более ранние сообщения", f"ticket:{ticket_id}:{offset + HISTORY_PAGE}{suffix}"),))
    if ticket["requester_id"] == user_id and ticket["status"] != "closed":
        actions.append((("✉️ Написать сообщение", f"follow:{ticket_id}"),))
    if ticket["requester_id"] == user_id:
        if ticket["status"] == "closed":
            actions.append((("Открыть повторно", f"owner_reopen:{ticket_id}:{ticket['status_version']}"),))
        else:
            actions.append((("Закрыть обращение", f"owner_close:{ticket_id}:{ticket['status_version']}"),))
    if admin and ticket["status"] != "closed":
        if ticket["status"] == "new":
            actions.append((("Взять в работу", f"status:{ticket_id}:in_progress{suffix}"),))
        actions.extend(((("Ответить", f"reply:{ticket_id}{suffix}"),), (("Закрыть обращение", f"close:{ticket_id}{suffix}"),)))
    default_list = f"list:{ticket['status']}:0" if admin else "mine:0"
    actions.append((("⬅️ К обращениям", origin or default_list),))
    if admin:
        actions.append((("К статусам", "queue"),))
    if admin:
        actions.append((("⬅️ В админ-панель", "panel"),))
    await message.answer(
        f"Обращение №{ticket_id} · {KINDS[ticket['kind']]} · {STATUSES[ticket['status']]}",
        reply_markup=keys(*actions, BACK),
    )


async def notify_admins(bot, admin_ids, group_id, ticket_id, kind, service_owner_id=None):
    admin_ids = set(await admin_access.staff("support", admin_ids, service_owner_id))
    text = f"Новое обращение №{ticket_id} · {KINDS[kind]}"
    if group_id is not None:
        try:
            if await is_private_admin_group(bot, group_id, admin_ids):
                await bot.send_message(group_id, f"{text}\nОткройте /support {ticket_id} в личном чате с ботом.")
                return
        except (TelegramAPIError, IsolationError) as error:
            logging.warning("Support group notice failed: %s", type(error).__name__)
    for admin_id in sorted(admin_ids):
        try:
            await bot.send_message(
                admin_id, text, reply_markup=keys((("Открыть обращение", f"ticket:{ticket_id}:0"),)),
            )
        except (TelegramAPIError, IsolationError) as error:
            logging.warning("Support admin notice failed: %s", type(error).__name__)


@router.callback_query(F.data.startswith("help:"))
async def support_action(callback: CallbackQuery, state: FSMContext, bot, support_admin_ids, support_group_id=None, service_owner_id=None):
    message = callback.message
    user_id = callback.from_user.id
    if not isinstance(message, Message) or message.chat.type != "private" or message.chat.id != user_id:
        await callback.answer("Недоступно.", show_alert=True)
        return
    parts = callback.data.split(":")[1:]
    action = parts[0]
    admin = await admin_access.allowed(user_id, "support", support_admin_ids, service_owner_id)
    if action in {"queue", "list", "status", "reply", "close"} and not admin:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    if parts == ["home"]:
        # A callback from an old help message must not clear a lead or offer draft.
        if await state.get_state() in Support.__all_states_names__:
            await state.clear()
        await menu(message, user_id, support_admin_ids)
    elif parts == ["resources"]:
        await message.answer("Как пользоваться сервисом", reply_markup=keys(
            (("📖 Инструкции по ролям", "guides"),),
            (("❔ Частые вопросы", "faq"), ("🎓 Попробовать на примере", "training")),
            (("🐞 Сообщить об ошибке", "new:bug"),), BACK,
        ))
    elif parts in (["guides"], ["training"]):
        prefix = "guide" if action == "guides" else "train"
        await message.answer("Выберите роль.", reply_markup=keys(
            (("📋 Заказчик", f"{prefix}:customer:0"), ("🚜 Исполнитель", f"{prefix}:provider:0")), BACK,
        ))
    elif action == "guide" and len(parts) == 3 and parts[1] in GUIDES and parts[2] in {str(index) for index in range(len(GUIDES[parts[1]]))}:
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
        await show_guide(message, parts[1], int(parts[2]))
    elif action == "train" and len(parts) == 3 and parts[1] in TRAINING and parts[2] == "0":
        await show_training(message, parts[1], 0)
    elif action == "try" and len(parts) == 4 and parts[1] in TRAINING and parts[2] in {str(index) for index in range(len(TRAINING[parts[1]]))}:
        if parts[3] == "1":
            try:
                await message.edit_reply_markup(reply_markup=None)
            except TelegramAPIError:
                pass
            await show_training(message, parts[1], int(parts[2]) + 1)
        elif parts[3] == "0":
            await message.answer("Попробуйте другой вариант.")
            await show_training(message, parts[1], int(parts[2]))
    elif parts == ["faq"]:
        await faq_list(message)
    elif action == "answer" and len(parts) == 2 and parts[1] in {str(i) for i in range(len(FAQ))}:
        title, answer, _ = FAQ[int(parts[1])]
        await message.answer(f"{title}\n\n{answer}", reply_markup=keys((("⬅️ К вопросам", "faq"),), BACK))
    elif parts == ["search"]:
        if await state.get_state() not in {None, Support.search.state}:
            await message.answer("Сначала завершите открытую анкету или перейдите в главное меню.")
            return
        await state.set_state(Support.search)
        await message.answer("Введите слова для поиска. Например: «оплата», «телефон», «кнопки».", reply_markup=keys(BACK))
    elif action == "new" and len(parts) == 2 and parts[1] in KINDS:
        if await state.get_state() is not None:
            await message.answer("Сначала завершите открытую анкету или перейдите в главное меню.")
            return
        await state.set_data({"support_kind": parts[1], "support_nonce": uuid4().hex})
        await state.set_state(Support.compose)
        text = "Опишите, что произошло, что ожидали и на каком шаге." if parts[1] == "bug" else "Опишите, с чем нужна помощь."
        await message.answer(f"{text}\nПри желании приложите скриншот. Не добавляйте пароли и коды.", reply_markup=keys(BACK))
    elif action == "edit" and len(parts) == 2 and await state.get_state() == Support.compose.state:
        if (await state.get_data()).get("support_nonce") == parts[1]:
            await state.update_data(support_nonce=uuid4().hex)
            await message.answer("Отправьте новый текст или скриншот. Прежнее вложение сохранится, пока вы его не замените.",
                                 reply_markup=keys(BACK))
    elif action == "send" and len(parts) == 2 and await state.get_state() == Support.compose.state:
        data = await state.get_data()
        if data.get("support_nonce") != parts[1] or not data.get("support_text"):
            return
        ticket_id, created = await store.create_ticket(
            user_id, data["support_kind"], parts[1], data["support_text"], data.get("support_photo"),
        )
        await state.clear()
        if created:
            await notify_admins(bot, support_admin_ids, support_group_id, ticket_id, data["support_kind"], service_owner_id)
        await message.answer(f"Обращение №{ticket_id} принято.", reply_markup=keys(
            (("Открыть обращение", f"ticket:{ticket_id}:0"),), BACK,
        ))
    elif parts == ["panel"] and admin:
        import admin_ui
        await admin_ui.menu(message, user_id, support_admin_ids, service_owner_id)
    elif parts == ["queue"]:
        totals = await store.counts()
        await message.answer("Очередь поддержки", reply_markup=keys(
            *(((f"{label} · {totals.get(status, 0)}", f"list:{status}:0"),) for status, label in STATUSES.items()),
            (("⬅️ В админ-панель", "panel"),), BACK,
        ))
    elif action == "mine" and len(parts) == 2 and page_number(parts[1], TICKET_PAGE) is not None:
        await ticket_list(message, user_id, offset=int(parts[1]))
    elif action == "list" and len(parts) == 3 and parts[1] in STATUSES and page_number(parts[2], TICKET_PAGE) is not None:
        await ticket_list(message, None, status=parts[1], offset=int(parts[2]))
    elif action == "ticket" and len(parts) >= 3 and positive_id(parts[1]) and page_number(parts[2], HISTORY_PAGE) is not None:
        await show_ticket(message, int(parts[1]), user_id, admin, int(parts[2]), ticket_list_origin(parts[3:], admin))
    elif action in {"owner_close", "owner_reopen", "owner_confirm"} and len(parts) == 3 and positive_id(parts[1]) and re.fullmatch(r"\d{1,18}", parts[2]):
        ticket = await store.ticket(int(parts[1]), user_id)
        if ticket is None:
            await message.answer("Обращение недоступно.", reply_markup=keys(BACK))
            return
        if ticket["status_version"] != int(parts[2]):
            await message.answer("Статус уже изменён. Откройте обращение снова.", reply_markup=keys(BACK))
            return
        if action == "owner_close":
            await message.answer(f"Закрыть обращение №{ticket['id']}?", reply_markup=keys(
                (("Подтвердить закрытие", f"owner_confirm:{ticket['id']}:{ticket['status_version']}"),),
                (("Вернуться", f"ticket:{ticket['id']}:0"),),
            ))
            return
        target = "closed" if action == "owner_confirm" else "new"
        changed = await store.requester_status(ticket["id"], user_id, target, int(parts[2]))
        if changed and target == "new":
            await notify_admins(bot, support_admin_ids, support_group_id, ticket["id"], ticket["kind"], service_owner_id)
        await message.answer("Статус обновлён." if changed else "Статус уже изменён.")
        await show_ticket(message, ticket["id"], user_id, admin)
    elif action == "close" and len(parts) >= 2 and positive_id(parts[1]):
        origin = ticket_list_origin(parts[2:], admin)
        suffix = f":{origin}" if origin else ""
        await message.answer(f"Закрыть обращение №{parts[1]}?", reply_markup=keys(
            (("Подтвердить закрытие", f"status:{parts[1]}:closed{suffix}"),),
            (("Вернуться", f"ticket:{parts[1]}:0{suffix}"),),
        ))
    elif action == "status" and len(parts) >= 3 and positive_id(parts[1]) and parts[2] in {"in_progress", "closed"}:
        changed = await store.change_status(int(parts[1]), parts[2], user_id)
        if changed:
            await admin_access.record(user_id, "support_status", target_id=int(parts[1]), details={"status": parts[2]})
        await message.answer(f"Статус обновлён: {STATUSES[parts[2]]}." if changed else "Статус уже изменён или обращение недоступно.")
        await show_ticket(message, int(parts[1]), user_id, admin, origin=ticket_list_origin(parts[3:], admin))
    elif action == "follow" and len(parts) == 2 and positive_id(parts[1]):
        ticket = await store.ticket(int(parts[1]), user_id)
        if not ticket or ticket["status"] == "closed" or await state.get_state() is not None:
            await message.answer("Обращение недоступно или открыта другая анкета.")
            return
        await state.set_data({"support_ticket": int(parts[1]), "support_nonce": uuid4().hex})
        await state.set_state(Support.requester_reply)
        await message.answer("Введите сообщение для поддержки.", reply_markup=keys(BACK))
    elif action == "reply" and len(parts) >= 2 and positive_id(parts[1]):
        if await state.get_state() is not None:
            await message.answer("Сначала завершите открытую анкету или перейдите в главное меню.")
            return
        ticket = await store.ticket(int(parts[1]))
        if ticket is None or ticket["status"] == "closed":
            await message.answer("Обращение закрыто или недоступно.")
            return
        await state.set_data({
            "support_ticket": int(parts[1]), "support_nonce": uuid4().hex,
            "support_origin": ticket_list_origin(parts[2:], admin),
        })
        await state.set_state(Support.reply)
        await message.answer(f"Введите ответ для обращения №{parts[1]}.", reply_markup=keys(BACK))


def page_number(value, size):
    if re.fullmatch(r"\d{1,7}", value) and int(value) % size == 0:
        return int(value)
    return None


def positive_id(value):
    return re.fullmatch(r"[1-9]\d{0,17}", value)


@router.message(Support.search)
async def search_faq(message: Message):
    if not message.text or not 1 <= len(message.text.strip()) <= 120:
        await message.answer("Введите запрос длиной до 120 символов.", reply_markup=keys(BACK))
        return
    await faq_list(message, message.text.strip())


@router.message(Support.compose)
async def compose_ticket(message: Message, state: FSMContext):
    text = (message.text or message.caption or "").strip()
    if len(text) > 1200 or (not text and not message.photo):
        await message.answer("Отправьте текст до 1200 символов и, при желании, скриншот.", reply_markup=keys(BACK))
        return
    fields = {"support_text": text} if text else {}
    if message.photo:
        fields["support_photo"] = message.photo[-1].file_id
    fields["support_nonce"] = uuid4().hex
    data = await state.update_data(**fields)
    if not data.get("support_text"):
        await message.answer("Скриншот добавлен. Теперь опишите проблему текстом.", reply_markup=keys(BACK))
        return
    await show_ticket_preview(message, data)


async def resume_support_step(message, state, current):
    data = await state.get_data()
    if current == Support.compose.state and data.get("support_text"):
        await show_ticket_preview(message, data)
    elif current == Support.compose.state:
        await message.answer("Опишите, с чем нужна помощь. При желании приложите скриншот. Не добавляйте пароли и коды.",
                             reply_markup=keys(BACK))
    else:
        await message.answer(f"Введите сообщение для обращения №{data['support_ticket']}.", reply_markup=keys(BACK))


async def show_ticket_preview(message, data):
    photo = "Скриншот приложен." if data.get("support_photo") else "Можно дополнить обращение скриншотом."
    await message.answer(f"Проверьте обращение\n\n{data['support_text']}\n\n{photo}", reply_markup=keys(
        (("✅ Отправить обращение", f"send:{data['support_nonce']}"),
         ("✏️ Изменить", f"edit:{data['support_nonce']}")), BACK,
    ))


@router.message(Support.requester_reply)
async def requester_reply(message: Message, state: FSMContext, bot, support_admin_ids, support_group_id=None, service_owner_id=None):
    text = (message.text or "").strip()
    if not 1 <= len(text) <= 1200:
        await message.answer("Введите сообщение длиной до 1200 символов.", reply_markup=keys(BACK))
        return
    data = await state.get_data()
    ticket_id = data["support_ticket"]
    actor_id = message.from_user.id
    ticket = await store.ticket(ticket_id, actor_id)
    if not ticket or not await store.add_requester_message(ticket_id, actor_id, data["support_nonce"], text):
        await message.answer("Обращение закрыто или сообщение уже сохранено.", reply_markup=keys(BACK))
        await state.clear()
        return
    await state.clear()
    await notify_admins(bot, support_admin_ids, support_group_id, ticket_id, ticket["kind"], service_owner_id)
    await message.answer("Сообщение сохранено в обращении.", reply_markup=keys(
        (("Открыть обращение", f"ticket:{ticket_id}:0"),), BACK,
    ))


@router.message(Support.reply)
async def reply_to_ticket(message: Message, state: FSMContext, bot, support_admin_ids, service_owner_id=None):
    if (message.chat.id != message.from_user.id
            or not await admin_access.allowed(message.from_user.id, "support", support_admin_ids, service_owner_id)):
        await message.answer("Нет доступа.")
        return
    text = (message.text or "").strip()
    if not 1 <= len(text) <= 1200:
        await message.answer("Введите ответ длиной до 1200 символов.", reply_markup=keys(BACK))
        return
    data = await state.get_data()
    ticket_id = data["support_ticket"]
    reply_id = await store.add_reply(ticket_id, message.from_user.id, data["support_nonce"], text)
    await state.clear()
    if not reply_id:
        await message.answer("Обращение закрыто или ответ уже сохранён.", reply_markup=keys(BACK))
        return
    await admin_access.record(message.from_user.id, "support_reply", target_id=ticket_id)
    status = await deliver_reply(bot, reply_id)
    origin = data.get("support_origin")
    suffix = f":{origin}" if origin else ""
    markup = keys((("К обращению", f"ticket:{ticket_id}:0{suffix}"),), BACK)
    if status == "sent":
        await message.answer("Ответ сохранён и отправлен.", reply_markup=markup)
    else:
        detail = DELIVERY.get(status, "результат отправки уточняется")
        await message.answer(f"Ответ сохранён в истории. {detail.capitalize()}.", reply_markup=markup)


def delivery_error_code(error):
    if isinstance(error, TelegramAPIError):
        description = error.message.casefold()
        for marker, code in (
            ("bot was blocked by the user", "bot_blocked"),
            ("chat not found", "chat_not_found"),
            ("user is deactivated", "user_deactivated"),
            ("can't initiate conversation", "conversation_not_started"),
        ):
            if marker in description:
                return code
    return type(error).__name__


async def deliver_reply(bot, reply_id):
    reply = await store.claim_delivery(reply_id)
    if reply is None:
        return None
    ticket_id = reply["ticket_id"]
    try:
        sent = await bot.send_message(
            reply["requester_id"], f"Ответ поддержки по обращению №{ticket_id}\n\n{reply['text']}",
            reply_markup=keys((("Открыть обращение", f"ticket:{ticket_id}:0"),)),
        )
    except (IsolationError, TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter) as error:
        status = "failed"
        await store.finish_delivery(reply_id, status, error_code=delivery_error_code(error))
    except (TelegramAPIError, TimeoutError) as error:
        # Telegram may have accepted a request whose response never reached us.
        status = "uncertain"
        await store.finish_delivery(reply_id, status, error_code=delivery_error_code(error))
    else:
        status = "sent"
        await store.finish_delivery(reply_id, status, telegram_message_id=sent.message_id)
    if status != "sent":
        logging.warning("Support reply %s for ticket %s: %s", reply_id, ticket_id, status)
    return status


def configure_delivery_worker(dispatcher):
    task = None

    async def drain(bot):
        while True:
            try:
                for reply_id in await store.queued_deliveries():
                    await deliver_reply(bot, reply_id)
            except Exception as error:
                logging.error("Support delivery worker: %s", type(error).__name__)
            await asyncio.sleep(5)

    async def start(bot):
        nonlocal task
        await store.recover_deliveries()
        task = asyncio.create_task(drain(bot))

    async def stop():
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    dispatcher.startup.register(start)
    dispatcher.shutdown.register(stop)
