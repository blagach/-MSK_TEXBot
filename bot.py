import asyncio
import logging
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv

from draft_storage import SQLiteDraftStorage, draft_slot, offer_draft_ids, offer_draft_slot

import admin_ui
import admin_access
import equipment_catalog
import support_storage
import support_ui
import storage
import provider_registry
import provider_ui
from provider_ui import AVAILABLE_LEADS, LEGACY_PROVIDER, MY_OFFERS, OFFER_DRAFT, section_keyboard
import provider_dispatch
import notification_reservations
import lead_journey
import lead_routing
import moderation_history
import lead_moderation
import lead_shadow_review
import payment_preferences

load_dotenv()


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: set[int]
    leads_channel_id: int
    payment_url: str
    bot_username: str
    test_notification_provider_ids: frozenset[int] = frozenset()
    support_group_id: int | None = None
    owner_id: int | None = None


def read_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "")
    admins = {int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip()}
    channel = os.getenv("LEADS_CHANNEL_ID", "")
    if not token or not admins or not channel:
        raise RuntimeError("Заполните BOT_TOKEN, ADMIN_IDS и LEADS_CHANNEL_ID в .env")
    support_group_id = int(os.environ["SUPPORT_GROUP_ID"]) if os.getenv("SUPPORT_GROUP_ID") else None
    if support_group_id is not None and (support_group_id >= 0 or support_group_id == int(channel)):
        raise RuntimeError("SUPPORT_GROUP_ID must identify a separate group")
    owner_id = int(os.environ["SERVICE_OWNER_ID"]) if os.getenv("SERVICE_OWNER_ID") else None
    if owner_id is not None and owner_id <= 0:
        raise RuntimeError("SERVICE_OWNER_ID must be a positive Telegram ID")
    return Settings(
        token,
        admins,
        int(channel),
        os.getenv("PAYMENT_URL", ""),
        os.getenv("BOT_USERNAME", "MSK_TEXBot").lstrip("@"),
        frozenset(int(value.strip()) for value in os.getenv("TEST_NOTIFICATION_PROVIDER_IDS", "").split(",") if value.strip()),
        support_group_id, owner_id,
    )


settings = read_settings()
router = Router()
router.message.filter(F.chat.type == "private")


class LeadForm(StatesGroup):
    equipment = State()
    equipment_type = State()
    equipment_other = State()
    region = State()
    area = State()
    place = State()
    date = State()
    schedule = State()
    map_link = State()
    details = State()
    attachments = State()
    payment = State()
    phone = State()
    review = State()
    edit = State()


class CloseLead(StatesGroup):
    reason = State()
    confirm = State()


class ProviderForm(StatesGroup):
    equipment = State()
    location = State()
    phone = State()


class OfferForm(StatesGroup):
    price = State()
    comment = State()
    note = State()
    phone = State()
    review = State()
    edit = State()
    cancel_confirmation = State()


OFFER_PROMPTS = {
    OfferForm.price: "Укажите вашу цену по заявке. Например: 28 000 ₽ за смену.",
    OfferForm.comment: "Коротко опишите условия: какая техника, подача, что включено.",
    OfferForm.note: "Добавьте комментарий к отклику или нажмите «Без комментария».",
    OfferForm.phone: (
        "📱 Укажите номер телефона для связи.\n\n"
        "Введите номер в формате +79123456789 или нажмите кнопку ниже "
        "для автоматической передачи номера из Telegram."
    ),
}
OFFER_BACK = "⬅️ Назад"
OFFER_HOME = "🏠 Главное меню"
LEAD_PROMPTS = {
    LeadForm.equipment: "Выберите категорию техники:",
    LeadForm.equipment_type: "Выберите вид техники:",
    LeadForm.equipment_other: "Укажите название нужной техники.",
    LeadForm.map_link: "Пришлите HTTPS-ссылку на карту или пропустите этот шаг.",
    LeadForm.schedule: "Укажите продолжительность и график смен или пропустите этот шаг.",
    LeadForm.region: "Выберите регион:",
    LeadForm.area: "Укажите район или населённый пункт (например, СЗАО или Химки).",
    LeadForm.place: "Укажите точный адрес, ориентир или ближайшее метро.",
    LeadForm.date: "На какую дату и время нужна техника?",
    LeadForm.details: "Опишите работу: объём, срок, груз, навесное оборудование, важные условия.",
    LeadForm.attachments: "Выберите дополнительное оборудование. Можно отметить несколько вариантов или написать свой. Затем нажмите «Готово».",
    LeadForm.payment: "Выберите форму оплаты:",
    LeadForm.phone: (
        "Введите номер телефона для связи без пробелов, скобок и дефисов. "
        "Например, +79123456789 или 89123456789."
    ),
}
ATTACHMENT_OPTIONS = ("Гидромолот", "Вилы", "Ковш", "Бур", "Стропы", "Захват")

LEAD_CHOICES = {
    LeadForm.region: (("Москва", "Московская область"), "region", LeadForm.area),
    LeadForm.payment: (
        (payment_preferences.CASH, payment_preferences.VAT, payment_preferences.NO_VAT, payment_preferences.ANY),
        "payment_method", LeadForm.phone,
    ),
}
FORM_STEPS = {step.state: step for step in (*LeadForm.__all_states__, *OfferForm.__all_states__)}
LEAD_DRAFT = "▶️ Продолжить заявку"
PROVIDER_ACTION_DRAFT = "▶️ Продолжить настройку исполнителя"
SUPPORT_ACTION_DRAFT = "▶️ Продолжить обращение"
CLOSING_ACTION_DRAFT = "▶️ Продолжить закрытие заявки"
ADMIN_ACTION_DRAFT = "▶️ Продолжить настройку прав"
CUSTOMER_ROLE = "📋 Я заказчик"
PROVIDER_ROLE = "🚜 Я исполнитель"
MY_LEADS = "📋 Мои заявки"
OFFER_EDIT_FIELDS = {
    "price": ("💰 Стоимость", OfferForm.price),
    "comment": ("📋 Условия", OfferForm.comment),
    "note": ("📝 Комментарий", OfferForm.note),
    "phone": ("📱 Телефон", OfferForm.phone),
}


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids or user_id == settings.owner_id


def main_menu(admin: bool = False, extra=()) -> ReplyKeyboardMarkup:
    actions = (support_ui.HELP_LABEL, admin_ui.ADMIN_LABEL) if admin else (support_ui.HELP_LABEL,)
    return section_keyboard((CUSTOMER_ROLE, PROVIDER_ROLE), actions, *extra)


async def admin_allowed(user_id, permission):
    return await admin_access.allowed(user_id, permission, settings.admin_ids, settings.owner_id)


async def admin_recipients(permission):
    return await admin_access.staff(permission, settings.admin_ids, settings.owner_id)


def respond_button(lead_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="🚜 Откликнуться", url=f"https://t.me/{settings.bot_username}?start=lead_{lead_id}",
    )


async def init_db() -> None:
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                customer_name TEXT,
                equipment TEXT NOT NULL,
                place TEXT NOT NULL,
                work_date TEXT NOT NULL,
                details TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS providers (
                telegram_id INTEGER PRIMARY KEY,
                name TEXT,
                equipment TEXT NOT NULL,
                location TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()
    await admin_access.init_schema()
    await storage.init_schema()
    await provider_registry.init_schema(storage.DB_PATH)
    await lead_routing.init_schema(storage.DB_PATH)
    await moderation_history.init_schema(storage.DB_PATH)
    await payment_preferences.init_schema(storage.DB_PATH)
    await notification_reservations.init_schema(storage.DB_PATH)
    await support_storage.init_schema()


PUBLIC_PHONE = re.compile(r"(?<![0-9])(?:\+7|[78])(?:[ ()-]*[0-9]){10}(?![0-9])")


def public_text(text):
    return re.sub(r"(?<!\w)#[\w-]+", "", PUBLIC_PHONE.sub("[контакт скрыт]", text))


def lead_text(lead: aiosqlite.Row, with_phone: bool = False) -> str:
    values = dict(lead)
    fields = [
        ("Техника", values.get("equipment_name") or lead["equipment"]),
        ("Регион", ", ".join(filter(None, (values.get("region"), values.get("area"))))),
        ("Адрес", lead["place"]), ("Карта", values.get("map_link")),
        ("Когда", lead["work_date"]), ("График смен", values.get("schedule")),
        ("Детали", lead["details"] or "не указаны"),
        ("Допоборудование", lead_journey.attachments_text(values)),
        ("Форма оплаты", values.get("payment_method") or "Не указана"),
    ]
    text = f"📌 Заявка №{lead['id']}\n" + "\n".join(f"{label}: {value}" for label, value in fields if value)
    return text + f"\nКонтакт заказчика: {lead['phone']}" if with_phone else public_text(text)


async def get_lead(lead_id: int):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT l.*, r.region, r.area, p.method AS payment_method, d.equipment_name, d.map_link, d.schedule,
                      a.selected_json AS attachments_json, a.custom_text AS attachments_custom
               FROM leads l LEFT JOIN lead_routing r ON r.lead_id=l.id
               LEFT JOIN lead_payment_preferences p ON p.lead_id=l.id
               LEFT JOIN lead_work_details d ON d.lead_id=l.id
               LEFT JOIN lead_attachments a ON a.lead_id=l.id WHERE l.id=?""", (lead_id,),
        ) as cursor:
            return await cursor.fetchone()


async def valid_input(message: Message, limit: int, phone: bool = False) -> bool:
    if not storage.valid_text(message.text, limit, phone):
        await message.answer("Введите телефон в формате +7XXXXXXXXXX или 8XXXXXXXXXX." if phone
                             else f"Отправьте текст от 1 до {limit} символов.")
        return False
    return True


async def notify(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramAPIError as exc:
        # Do not log personal data or the exception's request payload.
        logging.warning("Notification failed: %s", type(exc).__name__)
        return False


async def remove_form_keyboard(message: Message, message_id: int) -> None:
    try:
        await message.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=message_id)
    except TelegramAPIError as exc:
        logging.warning("Could not clear a form keyboard: %s", type(exc).__name__)


async def clear_form_keyboard(message: Message, state: FSMContext) -> None:
    message_id = (await state.get_data()).get("form_keyboard_id")
    if message_id is not None:
        await state.update_data(form_keyboard_id=None)
        await remove_form_keyboard(message, message_id)


async def send_form_panel(message: Message, state: FSMContext, prompt: str, rows: list) -> None:
    sent = await message.answer(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.update_data(form_keyboard_id=sent.message_id)


async def checkpoint_form(state: FSMContext, step: State, **fields) -> None:
    if isinstance(state.storage, SQLiteDraftStorage):
        data = await state.get_data()
        data.update(fields)
        await state.storage.checkpoint(state.key, step, data)
    else:
        if fields:
            await state.update_data(**fields)
        await state.set_state(step)


async def show_lead_step(message: Message, state: FSMContext, step: State, **fields) -> None:
    await clear_form_keyboard(message, state)
    await checkpoint_form(state, step, **fields)
    data = await state.get_data()
    options = lead_options(step, data)
    rows = equipment_catalog.button_rows(
        options, lambda index, label: InlineKeyboardButton(text=label, callback_data=f"lead_step:{index}"),
    )
    if step == LeadForm.attachments:
        selected = data.get("attachments", [])
        buttons = [
            InlineKeyboardButton(
                text=f"{'✅' if value in selected else '⬜'} {value}",
                callback_data=f"lead_attachment:pick:{index}",
            )
            for index, value in enumerate(ATTACHMENT_OPTIONS)
        ]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(text="Готово", callback_data="lead_attachment:done"),
                     InlineKeyboardButton(text="Не требуется", callback_data="lead_attachment:none")])
    if step == LeadForm.review:
        rows = [[InlineKeyboardButton(text="✅ Отправить на модерацию", callback_data="lead_review:send")],
                [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="lead_review:edit")]]
    if step == LeadForm.edit:
        rows = equipment_catalog.button_rows(
            tuple(LEAD_EDIT_FIELDS.values()),
            lambda index, label: InlineKeyboardButton(text=label, callback_data=f"lead_review:{index}"),
        )
    if step in (LeadForm.schedule, LeadForm.map_link):
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="lead_optional_skip")])
    if step == LeadForm.details:
        rows.append([InlineKeyboardButton(text="Пропустить описание", callback_data="lead_skip_details")])
    navigation = []
    if step != LeadForm.equipment:
        label = "⬅️ К категориям" if step == LeadForm.equipment_type and not data.get("lead_editing") else OFFER_BACK
        navigation.append(InlineKeyboardButton(text=label, callback_data="form_nav:back"))
    navigation.append(InlineKeyboardButton(text=OFFER_HOME, callback_data="form_nav:home"))
    rows.append(navigation)
    prompt = (
        lead_review_text(data) if step == LeadForm.review
        else "Что изменить?" if step == LeadForm.edit
        else LEAD_PROMPTS[step]
    )
    if step == LeadForm.attachments:
        prompt += "\n\nВыбрано: " + (lead_journey.attachments_text(data) or "ничего")
    await send_form_panel(message, state, prompt, rows)


def form_kind(current: str | None) -> str | None:
    if current in LeadForm.__all_states_names__:
        return "lead"
    if current in OfferForm.__all_states_names__:
        return "offer"
    return None


async def pause_form(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    kind = form_kind(current)
    if current in (*provider_ui.Register.__all_states_names__, *provider_ui.AddMachine.__all_states_names__,
                   *provider_ui.AlertPreferences.__all_states_names__):
        kind = "provider_action"
    elif current in admin_ui.ManageAdmins.__all_states_names__:
        kind = "admin_action"
    elif current in CloseLead.__all_states_names__:
        kind = "lead_closing"
    elif current in (support_ui.Support.compose.state, support_ui.Support.reply.state,
                     support_ui.Support.requester_reply.state):
        kind = "support_action"
    data = await state.get_data()
    await clear_form_keyboard(message, state)
    if kind:
        if current == provider_ui.Register.payment.state:
            await provider_ui.retire_payment_panel(message, state)
            data.pop("payment_panel", None)
        if current == provider_ui.AlertPreferences.select.state:
            await provider_ui.retire_inline_panel(message, state, "alert_panel")
        if data.get("machine_panel"):
            await remove_form_keyboard(message, data["machine_panel"])
        lead_id = storage.parse_id(f"lead_{data.get('lead_id')}", "lead_")
        draft = draft_slot(state, f"offer_{lead_id}" if kind == "offer" and lead_id else kind)
        if isinstance(state.storage, SQLiteDraftStorage):
            await state.storage.transfer(state.key, draft.key)
            return
        data.pop("form_keyboard_id", None)
        await draft.set_data(data)
        await draft.set_state(current)
    await state.clear()


async def menu_with_paused_actions(state):
    rows = []
    for kind, label in (("provider_action", PROVIDER_ACTION_DRAFT), ("support_action", SUPPORT_ACTION_DRAFT),
                        ("lead_closing", CLOSING_ACTION_DRAFT), ("admin_action", ADMIN_ACTION_DRAFT)):
        if await draft_slot(state, kind).get_state():
            rows.append((label,))
    return main_menu(await admin_allowed(state.key.user_id, "panel"), rows)


@router.message(F.text.in_({PROVIDER_ACTION_DRAFT, SUPPORT_ACTION_DRAFT, CLOSING_ACTION_DRAFT, ADMIN_ACTION_DRAFT}))
async def resume_auxiliary_action(message: Message, state: FSMContext):
    provider = message.text == PROVIDER_ACTION_DRAFT
    closing = message.text == CLOSING_ACTION_DRAFT
    admin_action = message.text == ADMIN_ACTION_DRAFT
    kind = "admin_action" if admin_action else ("lead_closing" if closing else ("provider_action" if provider else "support_action"))
    if admin_action and not await admin_allowed(message.from_user.id, "roles"):
        await message.answer("Нет доступа.")
        return
    draft = draft_slot(state, kind)
    current = await draft.get_state()
    if not current:
        await message.answer("Незавершённого действия нет.")
        return
    await pause_form(message, state)
    if isinstance(state.storage, SQLiteDraftStorage):
        await state.storage.transfer(draft.key, state.key)
    else:
        await state.set_data(await draft.get_data())
        await state.set_state(current)
        await draft.clear()
    if admin_action:
        await admin_ui.resume_admin_step(message, state)
    elif closing:
        await show_closing_step(message, state)
    elif provider:
        await provider_ui.resume_provider_step(message, state, current)
    else:
        await support_ui.resume_support_step(message, state, current)


async def restore_form_step(message: Message, state: FSMContext, current: str) -> None:
    step = FORM_STEPS[current]
    if form_kind(current) == "lead":
        await show_lead_step(message, state, step)
    else:
        await show_offer_step(message, state, step)


async def resume_draft(message: Message, state: FSMContext, kind: str, lead_id: int | None = None) -> bool:
    if kind == "offer" and lead_id is None:
        ids = await offer_draft_ids(state)
        if len(ids) != 1:
            return False
        lead_id = ids[0]
    draft = await offer_draft_slot(state, lead_id) if lead_id is not None else draft_slot(state, kind)
    current = await draft.get_state()
    if current is None:
        return False
    if kind == "lead" and form_kind(await state.get_state()) == kind:
        await open_navigation(message, state)
        return True
    await pause_form(message, state)
    if isinstance(state.storage, SQLiteDraftStorage):
        await state.storage.transfer(draft.key, state.key)
    else:
        await state.set_data(await draft.get_data())
        await state.set_state(current)
        await draft.clear()
    await message.answer("Продолжаем анкету.", reply_markup=ReplyKeyboardRemove())
    await restore_form_step(message, state, current)
    return True


async def back_in_form(message: Message, state: FSMContext, current: str) -> None:
    if form_kind(current) == "lead":
        data = await state.get_data()
        step = FORM_STEPS[current]
        if step == LeadForm.edit or data.get("lead_editing"):
            await show_lead_step(message, state, LeadForm.review, lead_editing=None)
            return
        if step == LeadForm.equipment_type and "equipment_group" not in data:
            await show_lead_step(message, state, LeadForm.equipment)
            return
        if step == LeadForm.region:
            previous = LeadForm.equipment_other if data.get("equipment_name") else (
                LeadForm.equipment_type if "equipment_group" in data else LeadForm.equipment
            )
        else:
            steps = (LeadForm.equipment, LeadForm.equipment_type, LeadForm.equipment_other, LeadForm.region,
                     LeadForm.area, LeadForm.place, LeadForm.map_link, LeadForm.date, LeadForm.schedule,
                     LeadForm.details, LeadForm.attachments, LeadForm.payment, LeadForm.phone, LeadForm.review)
            previous = steps[max(0, steps.index(step) - 1)]
        await show_lead_step(message, state, previous)
    elif current == OfferForm.phone.state:
        await offer_phone_back(message, state)
    elif current == OfferForm.comment.state and not (await state.get_data()).get("offer_editing"):
        await show_offer_step(message, state, OfferForm.price)
    elif current in (OfferForm.price.state, OfferForm.comment.state, OfferForm.note.state) and (await state.get_data()).get("offer_editing"):
        await next_offer_step(message, state, OfferForm.review)
    elif current == OfferForm.cancel_confirmation.state:
        data = await state.get_data()
        await restore_form_step(message, state, data["offer_cancel_from"])
    elif current == OfferForm.edit.state:
        await show_offer_step(message, state, OfferForm.review)
    elif current == OfferForm.review.state:
        await show_offer_step(message, state, OfferForm.phone)
    else:
        await restore_form_step(message, state, current)


@router.message(Command("menu"))
async def open_navigation(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if not form_kind(current):
        await show_main_menu(message, state)
        return
    await clear_form_keyboard(message, state)
    await send_form_panel(message, state, "Анкета открыта. Как продолжим?", [
        [InlineKeyboardButton(text="▶️ Продолжить анкету", callback_data="form_nav:continue")],
        [InlineKeyboardButton(text=OFFER_BACK, callback_data="form_nav:back"),
         InlineKeyboardButton(text=OFFER_HOME, callback_data="form_nav:home")],
    ])


@router.callback_query(F.data.startswith("form_nav:"))
async def form_navigation(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if (
        not form_kind(current) or not isinstance(callback.message, Message)
        or callback.message.message_id != (await state.get_data()).get("form_keyboard_id")
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    await callback.answer()
    action = callback.data.removeprefix("form_nav:")
    if action == "continue":
        await restore_form_step(callback.message, state, current)
    elif action == "back":
        await back_in_form(callback.message, state, current)
    elif action == "home":
        await show_main_menu(callback.message, state)


@router.message(F.text.in_({LEAD_DRAFT, OFFER_DRAFT}))
async def resume_form_from_menu(message: Message, state: FSMContext) -> None:
    if message.text == LEAD_DRAFT:
        if not await resume_draft(message, state, "lead"):
            await open_navigation(message, state)
        return
    await pause_form(message, state)
    ids = await offer_draft_ids(state)
    if len(ids) == 1:
        await open_offer(message, state, ids[0], state.key.user_id)
    else:
        await offer_draft_menu(message, state)


async def offer_draft_menu(message: Message, state: FSMContext, offset: int = 0) -> None:
    ids = await offer_draft_ids(state)
    if not ids:
        await message.answer("Сохранённых черновиков откликов нет.")
        return
    rows = [[InlineKeyboardButton(text=f"Заявка №{lead_id}", callback_data=f"offer_draft:open:{lead_id}")]
            for lead_id in ids[offset:offset + 10]]
    nav = []
    if offset:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"offer_draft:page:{offset - 10}"))
    if offset + 10 < len(ids):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"offer_draft:page:{offset + 10}"))
    await message.answer("Выберите черновик отклика.", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=rows + ([nav] if nav else []),
    ))


@router.callback_query(F.data.startswith("offer_draft:"))
async def offer_draft_action(callback: CallbackQuery, state: FSMContext) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.id != callback.from_user.id:
        await callback.answer("Недоступно.")
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or not re.fullmatch(r"[0-9]{1,18}", parts[2]):
        await callback.answer("Недоступно.")
        return
    action, number = parts[1], int(parts[2])
    if action == "page" and number % 10 == 0:
        await callback.answer()
        await offer_draft_menu(message, state, number)
    elif action == "open" and number in await offer_draft_ids(state):
        await callback.answer()
        await open_offer(message, state, number, callback.from_user.id)
    else:
        await callback.answer("Этот черновик недоступен.")


async def show_offer_step(message: Message, state: FSMContext, step: State, **fields) -> None:
    previous = await state.get_state()
    await clear_form_keyboard(message, state)
    await checkpoint_form(state, step, **fields)
    if step == OfferForm.phone:
        await message.answer(OFFER_PROMPTS[step], reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📲 Поделиться номером телефона", request_contact=True)],
                [KeyboardButton(text=OFFER_BACK), KeyboardButton(text=OFFER_HOME)],
            ],
            resize_keyboard=True, is_persistent=True,
        ))
        return
    if previous == OfferForm.phone.state:
        await message.answer(
            "Продолжаем отклик.",
            reply_markup=ReplyKeyboardRemove(),
        )

    data = await state.get_data()
    home = InlineKeyboardButton(text=OFFER_HOME, callback_data="offer_step:home")
    if step == OfferForm.review:
        prompt = (
            "📋 Проверьте ваш отклик\n\n"
            f"🚜 Номер заявки: {data['lead_id']}\n"
            f"💰 Стоимость: {data['price']}\n"
            f"{offer_terms_text(data['comment'], data.get('note'))}\n"
            f"📱 Телефон: {data['phone']}"
        )
        rows = [
            [InlineKeyboardButton(text="✅ Отправить отклик", callback_data="offer_step:send")],
            [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="offer_step:edit")],
            [InlineKeyboardButton(text="❌ Отменить отклик", callback_data="offer_step:cancel")],
            [home],
        ]
    elif step == OfferForm.cancel_confirmation:
        prompt = "Отменить незавершённый отклик?"
        rows = [
            [InlineKeyboardButton(text="❌ Да, отменить", callback_data="offer_step:cancel_confirm")],
            [InlineKeyboardButton(text="⬅️ Вернуться к отклику", callback_data="offer_step:back")],
            [home],
        ]
    else:
        rows = []
        if step == OfferForm.edit:
            prompt = "Выберите, какие данные изменить."
            rows.extend(
                [InlineKeyboardButton(text=label, callback_data=f"offer_step:{field}")]
                for field, (label, _) in OFFER_EDIT_FIELDS.items()
            )
        else:
            prompt = OFFER_PROMPTS[step]
            if step in (OfferForm.comment, OfferForm.note):
                rows.append([InlineKeyboardButton(text="Без комментария", callback_data="offer_step:skip")])
        navigation = []
        if step != OfferForm.price or data.get("offer_editing"):
            navigation.append(InlineKeyboardButton(text=OFFER_BACK, callback_data="offer_step:back"))
        rows.append([*navigation, home])
    await send_form_panel(message, state, prompt, rows)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if form_kind(current) == "offer":
        await confirm_offer_cancellation(message, state, current)
        return
    await clear_form_keyboard(message, state)
    await state.clear()
    await message.answer("Заполнение отменено.", reply_markup=main_menu(is_admin(state.key.user_id)))


@router.message(CommandStart(deep_link=True))
async def start_offer(message: Message, state: FSMContext, command: CommandObject) -> None:
    lead_id = storage.parse_id(command.args, "lead_")
    await open_offer(message, state, lead_id, message.from_user.id)


async def open_offer(message: Message, state: FSMContext, lead_id: int | None, user_id: int) -> None:
    lead = await get_lead(lead_id) if lead_id else None
    if not lead or lead["status"] != "published":
        await message.answer("Ссылка на заявку устарела или заявка недоступна.")
        await open_navigation(message, state)
        return
    if await storage.has_offer(lead_id, user_id):
        await message.answer(f"Вы уже откликнулись на заявку №{lead_id}.")
        return
    current = await state.get_state()
    if form_kind(current) == "offer" and (await state.get_data()).get("lead_id") == lead_id:
        await restore_form_step(message, state, current)
        return
    if await resume_draft(message, state, "offer", lead_id):
        return
    await pause_form(message, state)
    await message.answer(f"Отклик на заявку №{lead_id}.", reply_markup=ReplyKeyboardRemove())
    await show_offer_step(message, state, OfferForm.price, lead_id=lead_id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await message.answer(
        "Здравствуйте! Здесь можно быстро оставить заявку на аренду строительной или дорожной техники в Москве и МО.",
        reply_markup=await menu_with_paused_actions(state),
    )


@router.message(F.text == OFFER_HOME)
async def show_main_menu(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await message.answer("Главное меню MSK_TEX. Выберите нужный раздел:",
                         reply_markup=await menu_with_paused_actions(state))


@router.message(Command("support"), F.chat.type == "private")
@router.message(F.text == support_ui.HELP_LABEL, F.chat.type == "private")
async def open_support(message: Message, state: FSMContext, command: CommandObject | None = None) -> None:
    user_id = message.from_user.id
    await pause_form(message, state)
    await message.answer("Выберите раздел ниже.", reply_markup=main_menu(is_admin(user_id)))
    if command and command.args and await admin_allowed(user_id, "support") and support_ui.positive_id(command.args):
        await support_ui.show_ticket(message, int(command.args), user_id, True)
    else:
        await support_ui.menu(message, user_id, settings.admin_ids)


@router.message(Command("admin"), F.chat.type == "private")
@router.message(F.text == admin_ui.ADMIN_LABEL, F.chat.type == "private")
async def open_admin_panel(message: Message, state: FSMContext) -> None:
    if not await admin_allowed(message.from_user.id, "panel"):
        await admin_access.record(message.from_user.id, "admin_denied", result="denied")
        await message.answer("Нет доступа.")
        return
    await pause_form(message, state)
    await message.answer("Выберите отчёт ниже.", reply_markup=main_menu(True))
    await admin_ui.menu(message, message.from_user.id, settings.admin_ids, settings.owner_id)


@router.message(F.text.in_({CUSTOMER_ROLE, "🧾 Заказчик"}))
async def customer_menu(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    rows = [("📝 Оставить заявку", MY_LEADS)]
    if await draft_slot(state, "lead").get_state():
        rows.append((LEAD_DRAFT,))
    await message.answer("Меню заказчика.", reply_markup=section_keyboard(*rows, (OFFER_HOME,)))


@router.message(F.text.in_({PROVIDER_ROLE, "🛠 Исполнитель"}))
async def executor_menu(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await message.answer("Меню исполнителя.",
                         reply_markup=await provider_ui.executor_keyboard(message.from_user.id, state))


LEAD_STATUS = {
    "new": "на модерации", "publishing": "публикуется", "published": "опубликована",
    "rejected": "отклонена", "closed": "закрыта", "archived": "в архиве",
}


def lead_key(label, data):
    return InlineKeyboardButton(text=label, callback_data=f"lc:{data}")


async def list_customer_leads(message, user_id, before=2**63 - 1, admin=False):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        query = """SELECT id, equipment, status FROM leads WHERE id < ? AND
                   ((? = 1 AND status IN ('new','publishing','published')) OR (? = 0 AND customer_id=?))
                   ORDER BY id DESC LIMIT 9"""
        async with db.execute(query, (before, admin, admin, user_id)) as cursor:
            leads = await cursor.fetchall()
    prefix = "admin" if admin else "list"
    rows = [[lead_key(f"№{number} · {equipment} · {LEAD_STATUS.get(status, status)}", f"open:{number}")]
            for number, equipment, status in leads[:8]]
    if len(leads) > 8:
        rows.append([lead_key("Ещё заявки", f"{prefix}:{leads[7][0]}")])
    if before != 2**63 - 1:
        rows.append([lead_key("К началу списка", f"{prefix}:0")])
    await message.answer("Заявки для модерации и опубликованные" if admin else "Ваши заявки",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)
    if not leads:
        await message.answer("Заявок пока нет.")


@router.message(F.text == MY_LEADS)
async def my_leads(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await list_customer_leads(message, message.from_user.id)


@router.message(F.text.in_({AVAILABLE_LEADS, "🔎 Смотреть заявки", "🚜 Смотреть заявки"}))
async def available_leads(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await show_matching_leads(message, message.from_user.id)


async def show_matching_leads(message, user_id, before=2**63 - 1):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await provider_registry.register_area_key(db)
        predicate = provider_registry.alert_match("r.category", "r.region", "r.area")
        async with db.execute(
            f"""SELECT l.*, r.category, r.region, r.area, p2.method AS payment_method,
                      d.equipment_name, d.map_link, d.schedule FROM leads l
               JOIN lead_routing r ON r.lead_id=l.id
               LEFT JOIN lead_work_details d ON d.lead_id=l.id
               LEFT JOIN lead_payment_preferences p2 ON p2.lead_id=l.id
               WHERE l.status='published' AND l.id<? AND EXISTS (
                   SELECT 1 FROM provider_profiles p
                   WHERE p.telegram_id=? AND p.blocked=0
                   AND {predicate}
               ) ORDER BY l.id DESC LIMIT 9""",
            (before, user_id),
        ) as cursor:
            leads = await cursor.fetchall()
    for lead in leads[:8]:
        await message.answer(lead_text(lead), reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[respond_button(lead["id"])]],
        ))
    if len(leads) > 8:
        await message.answer("Другие подходящие заявки", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[lead_key("Следующая страница", f"match:{leads[7]['id']}")]],
        ))
    if not leads:
        await message.answer("Подходящих заявок пока нет. Все опубликованные заявки доступны в канале.")


@router.callback_query(F.data.startswith("lc:"))
async def lead_card_action(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts = callback.data.split(":")
    if len(parts) not in (3, 4) or not isinstance(callback.message, Message) or callback.message.chat.type != "private":
        await callback.answer("Карточка недоступна.")
        return
    _, action, value = parts[:3]
    lead_id = storage.parse_id("lead_" + value, "lead_")
    user_id = callback.from_user.id
    admin = await admin_allowed(user_id, "moderate")
    message = callback.message
    if message.chat.id != user_id:
        await callback.answer("Карточка недоступна.")
        return
    if action in {"admin", "list", "match", "myoffers"} and (lead_id or value == "0"):
        if action == "admin" and not admin:
            await callback.answer("Нет доступа.")
            return
        await callback.answer()
        if action == "myoffers":
            await show_my_offers(message, user_id, lead_id or 2**63 - 1)
        elif action == "match":
            await show_matching_leads(message, user_id, lead_id or 2**63 - 1)
        else:
            await list_customer_leads(message, user_id, lead_id or 2**63 - 1, action == "admin")
        return
    lead = await get_lead(lead_id) if lead_id else None
    if not lead or (user_id != lead["customer_id"] and not admin):
        await callback.answer("Заявка недоступна.")
        return
    owner = user_id == lead["customer_id"]
    await callback.answer()
    if action == "open":
        if admin:
            await admin_access.mark_lead_view(user_id, lead_id)
            await admin_access.record(user_id, "lead_view", target_id=lead_id)
        await show_customer_lead(message, lead, owner, admin)
    elif action == "offers" and owner:
        before = storage.parse_id(parts[3], "") if len(parts) == 4 else 2**63 - 1
        if before:
            await show_customer_offers(message, lead, before)
    elif action == "pick" and owner and len(parts) == 4:
        offer_id = storage.parse_id(parts[3], "")
        if offer_id and await lead_journey.change_lifecycle(storage.DB_PATH, lead_id, user_id, offer_id=offer_id):
            await message.answer(f"Исполнитель по заявке №{lead_id} выбран.")
        else:
            await message.answer("Выбор недоступен или исполнитель уже выбран.")
    elif action == "close" and lead["status"] == "published":
        await pause_form(message, state)
        await state.set_state(CloseLead.reason)
        await state.update_data(closing_lead=lead_id)
        await show_closing_step(message, state)
    elif action == "repeat" and owner:
        if form_kind(await state.get_state()) == "lead" or await draft_slot(state, "lead").get_state():
            await message.answer("Сначала завершите или отмените текущий черновик заявки.")
            return
        await pause_form(message, state)
        data = {key: lead[key] for key in (
            "equipment", "equipment_name", "region", "area", "place", "map_link", "work_date",
            "schedule", "details", "payment_method", "phone",
        )}
        data["attachments"] = json.loads(lead["attachments_json"] or "[]")
        data["attachments_custom"] = lead["attachments_custom"] or ""
        step = LeadForm.review if data["region"] and data["area"] else LeadForm.region
        await show_lead_step(message, state, step, **data, draft_id=uuid4().hex)


async def show_customer_lead(message, lead, owner, admin):
    lead_id = lead["id"]
    status = lead["status"]
    rows = []
    if owner:
        rows.append([lead_key("Отклики", f"offers:{lead_id}"), lead_key("Повторить заявку", f"repeat:{lead_id}")])
    if status == "published":
        rows.append([lead_key("Закрыть заявку", f"close:{lead_id}")])
    if admin and status == "new":
        rows.append([InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{lead_id}"),
                     InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{lead_id}")])
    rows.append([lead_key("К заявкам", "list:0" if owner else "admin:0")])
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute("SELECT reason FROM lead_lifecycle_events WHERE lead_id=? AND action='closed'",
                              (lead_id,)) as cursor:
            closing = await cursor.fetchone()
    closing_text = f"\nПричина закрытия: {closing[0]}" if closing else ""
    await message.answer(f"{lead_text(lead, True)}\nСтатус: {LEAD_STATUS.get(status, status)}{closing_text}",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def show_customer_offers(message, lead, before):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT o.*, n.note, e.offer_id AS selected FROM offers o
               LEFT JOIN offer_notes n ON n.offer_id=o.id
               LEFT JOIN lead_lifecycle_events e
               ON e.lead_id=o.lead_id AND e.action='selected'
               WHERE o.lead_id=? AND o.id<? ORDER BY o.id DESC LIMIT 9""", (lead["id"], before),
        ) as cursor:
            offers = await cursor.fetchall()
    for offer in offers[:8]:
        buttons = [[InlineKeyboardButton(text="📞 Показать номер", callback_data=f"offer_call:{offer['id']}")]]
        if offer["selected"] is None and lead["status"] == "published":
            buttons.append([lead_key("Выбрать исполнителя", f"pick:{lead['id']}:{offer['id']}")])
        await message.answer(
            f"{offer['provider_name']} · {offer['price']}\n{offer_terms_text(offer['comment'], offer['note'])}"
            + ("\n✅ Исполнитель выбран" if offer["selected"] == offer["id"] else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    buttons = [[lead_key("К карточке заявки", f"open:{lead['id']}")]]
    if len(offers) > 8:
        buttons.insert(0, [lead_key("Ещё отклики", f"offers:{lead['id']}:{offers[7]['id']}")])
    await message.answer("Отклики по заявке" if offers else "Откликов пока нет.",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def show_closing_step(message, state):
    await clear_form_keyboard(message, state)
    data = await state.get_data()
    if await state.get_state() == CloseLead.confirm.state:
        rows = [
            [InlineKeyboardButton(text="✅ Подтвердить закрытие", callback_data="lead_close:send")],
            [InlineKeyboardButton(text="✏️ Изменить причину", callback_data="lead_close:edit"),
             InlineKeyboardButton(text="Отмена", callback_data="lead_close:cancel")],
            [InlineKeyboardButton(text=OFFER_HOME, callback_data="lead_close:home")],
        ]
        await send_form_panel(message, state,
                              f"Закрыть заявку №{data['closing_lead']}?\nПричина: {data['closing_reason']}", rows)
    else:
        await message.answer("Укажите причину закрытия заявки.",
                             reply_markup=section_keyboard((OFFER_HOME,)))


@router.message(CloseLead.reason)
async def close_lead_reason(message: Message, state: FSMContext):
    if not await valid_input(message, 500):
        return
    await state.update_data(closing_reason=message.text.strip())
    await state.set_state(CloseLead.confirm)
    await show_closing_step(message, state)


@router.callback_query(F.data.startswith("lead_close:"))
async def close_lead_confirmation(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    message = callback.message
    if (await state.get_state() != CloseLead.confirm.state or not is_form_panel(callback, data)
            or message.chat.type != "private" or message.chat.id != callback.from_user.id):
        await callback.answer("Этот шаг уже неактуален.")
        return
    action = callback.data.removeprefix("lead_close:")
    if action not in {"send", "edit", "cancel", "home"}:
        await callback.answer("Неизвестное действие.")
        return
    await callback.answer()
    if action == "home":
        await show_main_menu(message, state)
        return
    await clear_form_keyboard(message, state)
    if action == "edit":
        await state.set_state(CloseLead.reason)
        await show_closing_step(message, state)
        return
    lead_id = data["closing_lead"]
    user_id = callback.from_user.id
    if action == "cancel":
        await state.clear()
        await message.answer("Закрытие отменено.", reply_markup=main_menu(is_admin(user_id)))
        return
    changed = await lead_journey.change_lifecycle(
        storage.DB_PATH, lead_id, user_id, admin=await admin_allowed(user_id, "moderate"), reason=data["closing_reason"],
    )
    await admin_access.record(user_id, "lead_close", target_id=lead_id,
                              result="success" if changed else "not_changed")
    await state.clear()
    notice = ""
    if changed:
        lead = await get_lead(lead_id)
        if lead["customer_id"] != user_id:
            delivered = await notify(bot, lead["customer_id"],
                                           f"Заявка №{lead_id} закрыта администратором.\nПричина: {data['closing_reason']}")
            notice = "\nУведомление принято Telegram." if delivered else "\nУведомление заказчику не отправлено."
    await message.answer(
        (f"Заявка №{lead_id} закрыта. Причина сохранена." + notice) if changed
        else "Заявка недоступна или уже закрыта.", reply_markup=main_menu(is_admin(user_id)),
    )


@router.message(F.text == MY_OFFERS)
async def my_offers(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await show_my_offers(message, message.from_user.id)


async def show_my_offers(message, user_id, before=2**63 - 1):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            f"""SELECT o.id, o.lead_id, o.price, o.comment, n.note, o.delivery_status,
                      {lead_journey.OFFER_RESULT} AS outcome
               FROM offers o JOIN leads l ON l.id=o.lead_id
               LEFT JOIN offer_notes n ON n.offer_id=o.id
               LEFT JOIN lead_lifecycle_events e ON e.lead_id=o.lead_id AND e.action='selected'
               WHERE o.provider_id=? AND o.id<? ORDER BY o.id DESC LIMIT 9""",
            (user_id, before),
        ) as cursor:
            offers = await cursor.fetchall()
    statuses = {"sent": "отправлен", "pending": "доставка ожидается", "unconfirmed": "доставка не подтверждена"}
    if not offers:
        await message.answer("У вас пока нет откликов.")
        return
    for _, lead_id, price, comment, note, status, outcome in offers[:8]:
        await message.answer(
            f"Отклик на заявку №{lead_id}\nСтоимость: {price}\n{offer_terms_text(comment, note)}"
            f"\nСтатус: {outcome}\nДоставка: {statuses.get(status, status)}"
        )


    if len(offers) > 8:
        await message.answer("История откликов", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[lead_key("Ещё отклики", f"myoffers:{offers[7][0]}")]],
        ))


@router.message(F.text == "📝 Оставить заявку")
async def lead_start(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if form_kind(current) == "lead":
        await restore_form_step(message, state, current)
    elif not await resume_draft(message, state, "lead"):
        await pause_form(message, state)
        await message.answer("Оформляем заявку.", reply_markup=ReplyKeyboardRemove())
        draft_fields = {"draft_id": uuid4().hex} if isinstance(state.storage, SQLiteDraftStorage) else {}
        await show_lead_step(message, state, LeadForm.equipment, **draft_fields)


def lead_options(step, data):
    if step == LeadForm.equipment:
        return tuple(name for name, _ in equipment_catalog.GROUPS)
    if step == LeadForm.equipment_type:
        return equipment_catalog.GROUPS[data["equipment_group"]][1] if "equipment_group" in data else ()
    return LEAD_CHOICES[step][0] if step in LEAD_CHOICES else ()


async def advance_lead(message, state, next_step, **fields):
    data = await state.get_data()
    if data.get("lead_editing"):
        editing_equipment = data["lead_editing"] == LeadForm.equipment.state
        if not editing_equipment or next_step == LeadForm.region:
            next_step = LeadForm.review
            fields["lead_editing"] = None
    await show_lead_step(message, state, next_step, **fields)


async def apply_lead_choice(message: Message, state: FSMContext, step: State, choice: str) -> None:
    if step == LeadForm.equipment:
        await show_lead_step(message, state, LeadForm.equipment_type, equipment_group=lead_options(step, {}).index(choice))
        return
    if step == LeadForm.equipment_type:
        next_step = LeadForm.equipment_other if choice == equipment_catalog.OTHER else LeadForm.region
        if next_step == LeadForm.equipment_other:
            await show_lead_step(message, state, next_step)
        else:
            await advance_lead(message, state, next_step, equipment=choice, equipment_name="")
        return
    _, field, next_step = LEAD_CHOICES[step]
    await advance_lead(message, state, next_step, **{field: choice})


@router.callback_query(F.data.startswith("lead_step:"))
async def lead_choice(callback: CallbackQuery, state: FSMContext) -> None:
    step = FORM_STEPS.get(await state.get_state())
    suffix = callback.data.removeprefix("lead_step:")
    data = await state.get_data()
    if (
        not lead_options(step, data) or not isinstance(callback.message, Message)
        or callback.message.message_id != data.get("form_keyboard_id")
        or not re.fullmatch(r"[0-9]{1,3}", suffix)
        or int(suffix) >= len(lead_options(step, data))
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    await callback.answer()
    await apply_lead_choice(callback.message, state, step, lead_options(step, data)[int(suffix)])


@router.message(LeadForm.equipment)
async def lead_equipment(message: Message, state: FSMContext) -> None:
    if message.text not in provider_registry.CATEGORIES:
        await message.answer("Выберите категорию техники кнопкой из списка или введите /menu.")
        return
    if message.text == equipment_catalog.OTHER:
        await show_lead_step(message, state, LeadForm.equipment_other)
    else:
        await advance_lead(message, state, LeadForm.region, equipment=message.text, equipment_name="")


@router.message(LeadForm.equipment_other)
async def lead_equipment_other(message: Message, state: FSMContext) -> None:
    if await valid_input(message, 120):
        await advance_lead(message, state, LeadForm.region, equipment=equipment_catalog.OTHER,
                           equipment_name=message.text.strip())


@router.message(LeadForm.region)
async def lead_region(message: Message, state: FSMContext) -> None:
    if message.text not in provider_registry.REGIONS:
        await message.answer("Выберите Москву или Московскую область. Чтобы вернуть кнопки, введите /menu.")
        return
    await apply_lead_choice(message, state, LeadForm.region, message.text)


@router.message(LeadForm.area)
async def lead_area(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 120):
        return
    await advance_lead(message, state, LeadForm.place, area=message.text.strip())


@router.message(LeadForm.place)
async def lead_place(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 300, phone=False):
        return
    await advance_lead(message, state, LeadForm.map_link, place=message.text)


@router.message(LeadForm.date)
async def lead_date(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 100, phone=False):
        return
    await advance_lead(message, state, LeadForm.schedule, work_date=message.text)


@router.message(LeadForm.map_link)
async def lead_map_link(message: Message, state: FSMContext) -> None:
    value = message.text or ""
    if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?:[/?#][^\s]*)?", value) or len(value) > 500:
        await message.answer("Нужна HTTPS-ссылка на карту. Без ссылки нажмите «Пропустить».")
        return
    await advance_lead(message, state, LeadForm.date, map_link=value)


@router.message(LeadForm.schedule)
async def lead_schedule(message: Message, state: FSMContext) -> None:
    if await valid_input(message, 160):
        await advance_lead(message, state, LeadForm.details, schedule=message.text.strip())


@router.callback_query(F.data == "lead_optional_skip")
async def skip_lead_optional(callback: CallbackQuery, state: FSMContext) -> None:
    step = FORM_STEPS.get(await state.get_state())
    data = await state.get_data()
    if step not in (LeadForm.map_link, LeadForm.schedule) or not is_form_panel(callback, data):
        await callback.answer("Этот шаг уже неактуален.")
        return
    await callback.answer()
    if step == LeadForm.map_link:
        await advance_lead(callback.message, state, LeadForm.date, map_link="")
    else:
        await advance_lead(callback.message, state, LeadForm.details, schedule="")


@router.callback_query(F.data == "lead_skip_details")
async def skip_lead_details(callback: CallbackQuery, state: FSMContext) -> None:
    if (
        await state.get_state() != LeadForm.details.state or not isinstance(callback.message, Message)
        or callback.message.message_id != (await state.get_data()).get("form_keyboard_id")
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    await callback.answer()
    await advance_lead(callback.message, state, LeadForm.attachments, details="")


@router.message(LeadForm.details)
async def lead_details(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 1500, phone=False):
        return
    await advance_lead(message, state, LeadForm.attachments, details=message.text)


@router.callback_query(F.data.startswith("lead_attachment:"))
async def lead_attachment_action(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await state.get_state() != LeadForm.attachments.state or not is_form_panel(callback, data):
        await callback.answer("Этот шаг уже неактуален.")
        return
    action = callback.data.removeprefix("lead_attachment:")
    if action == "done":
        await callback.answer()
        await advance_lead(callback.message, state, LeadForm.payment)
    elif action == "none":
        await callback.answer()
        await advance_lead(callback.message, state, LeadForm.payment, attachments=[], attachments_custom="")
    elif re.fullmatch(r"pick:[0-5]", action):
        await callback.answer()
        value = ATTACHMENT_OPTIONS[int(action[-1])]
        selected = set(data.get("attachments", [])) ^ {value}
        await show_lead_step(callback.message, state, LeadForm.attachments,
                             attachments=[item for item in ATTACHMENT_OPTIONS if item in selected])
    else:
        await callback.answer("Неизвестное действие.")


@router.message(LeadForm.attachments)
async def lead_attachment_custom(message: Message, state: FSMContext):
    if await valid_input(message, 300):
        await show_lead_step(message, state, LeadForm.attachments, attachments_custom=message.text.strip())


@router.message(LeadForm.payment)
async def lead_payment(message: Message, state: FSMContext) -> None:
    if not message.text or message.text not in payment_preferences.METHODS | {payment_preferences.ANY}:
        await message.answer("Выберите форму оплаты кнопкой из списка.")
        return
    await apply_lead_choice(message, state, LeadForm.payment, message.text)


async def save_lead_from_draft(customer: User, phone: str, data: dict) -> tuple[int, bool]:
    draft_id = data.get("draft_id")
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        if draft_id:
            async with db.execute("SELECT lead_id FROM lead_draft_submissions WHERE draft_id=?", (draft_id,)) as cursor:
                previous = await cursor.fetchone()
            if previous:
                return previous[0], False
        cursor = await db.execute(
            """INSERT INTO leads(customer_id, customer_name, equipment, place, work_date, details, phone, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (customer.id, customer.full_name, data["equipment"], data["place"], data["work_date"],
             data["details"], phone, datetime.now(timezone.utc).isoformat()),
        )
        lead_id = cursor.lastrowid
        await payment_preferences.write_lead(db, lead_id, data.get("payment_method") or payment_preferences.ANY)
        await lead_routing.write_lead(db, lead_id, data["equipment"], data["region"], data["area"])
        await lead_journey.write_details(db, lead_id, data, getattr(customer, "username", None))
        await lead_journey.write_attachments(db, lead_id, data)
        await lead_journey.record_event(db, lead_id, "submitted", actor_id=customer.id)
        if draft_id:
            await db.execute("INSERT INTO lead_draft_submissions VALUES (?,?)", (draft_id, lead_id))
        await db.commit()
        return lead_id, True


LEAD_EDIT_FIELDS = {
    LeadForm.equipment: "Техника", LeadForm.region: "Регион", LeadForm.area: "Район",
    LeadForm.place: "Адрес", LeadForm.map_link: "Ссылка на карту", LeadForm.date: "Дата и время",
    LeadForm.schedule: "График смен", LeadForm.details: "Описание",
    LeadForm.attachments: "Допоборудование",
    LeadForm.payment: "Оплата", LeadForm.phone: "Телефон",
}


def lead_review_text(data):
    fields = ("equipment_name", "region", "area", "place", "map_link", "work_date", "schedule", "details",
              "attachments", "payment_method", "phone")
    lines = []
    for label, field in zip(LEAD_EDIT_FIELDS.values(), fields):
        value = lead_journey.attachments_text(data) if field == "attachments" else data.get(field)
        if field == "equipment_name" and not value:
            value = data.get("equipment")
        lines.append(f"{label}: {value or 'не указано'}")
    return "📋 Предпросмотр заявки\n\n" + "\n".join(lines)


def is_form_panel(callback, data):
    return isinstance(callback.message, Message) and callback.message.message_id == data.get("form_keyboard_id")


@router.message(LeadForm.phone)
async def lead_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        phone = provider_registry._validate_phone(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await advance_lead(message, state, LeadForm.review, phone=phone)


@router.callback_query(F.data.startswith("lead_review:"))
async def lead_review_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    step = FORM_STEPS.get(await state.get_state())
    if step not in (LeadForm.review, LeadForm.edit) or not is_form_panel(callback, data):
        await callback.answer("Этот шаг уже неактуален.")
        return
    action = callback.data.removeprefix("lead_review:")
    await callback.answer()
    if step == LeadForm.review and action == "edit":
        await show_lead_step(callback.message, state, LeadForm.edit)
    elif step == LeadForm.review and action == "send":
        await submit_lead(callback.message, callback.from_user, state, bot, data)
    elif step == LeadForm.edit and action.isascii() and action.isdecimal() and len(action) <= 2:
        fields = tuple(LEAD_EDIT_FIELDS)
        if int(action) < len(fields):
            field = fields[int(action)]
            await show_lead_step(callback.message, state, field, lead_editing=field.state)


async def submit_lead(message, customer, state, bot, data):
    lead_id, created = await save_lead_from_draft(customer, data["phone"], data)
    if not created:
        await clear_form_keyboard(message, state)
        await state.clear()
        await message.answer(f"Заявка №{lead_id} уже сохранена.", reply_markup=main_menu(is_admin(customer.id)))
        return
    lead = await get_lead(lead_id)
    # Shadow evaluation only: never changes the manual approval workflow.
    shadow = await lead_shadow_review.evaluate(
        customer.id, equipment=data["equipment"],
        region=data["region"], area=data["area"], place=data["place"],
        work_date=data["work_date"], details=data["details"],
        db_path=storage.DB_PATH, enabled=False, exclude_lead_id=lead_id,
    )
    logging.info("Lead %s shadow route: %s", lead_id, shadow.route)
    markup = InlineKeyboardMarkup(inline_keyboard=[[ 
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{lead_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{lead_id}"),
    ]])
    for admin_id in await admin_recipients("moderate"):
        await notify(bot, admin_id, "Новая заявка на проверку:\n\n" + lead_text(lead, True), reply_markup=markup)
    await clear_form_keyboard(message, state)
    await state.clear()
    await message.answer("Спасибо! Проверим заявку и передадим её подходящим исполнителям.", reply_markup=main_menu(is_admin(customer.id)))


@router.message(F.text == LEGACY_PROVIDER)
async def provider_start(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await state.set_state(ProviderForm.equipment)
    await message.answer("Какую технику вы предоставляете?", reply_markup=ReplyKeyboardRemove())


@router.callback_query(F.data == "provider_legacy:edit")
async def edit_legacy_provider(callback: CallbackQuery, state: FSMContext) -> None:
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute("SELECT 1 FROM providers WHERE telegram_id=?", (callback.from_user.id,)) as cursor:
            legacy = await cursor.fetchone()
    if not legacy or not isinstance(callback.message, Message):
        await callback.answer("Быстрая регистрация недоступна.", show_alert=True)
        return
    await callback.answer()
    await provider_start(callback.message, state)


@router.message(ProviderForm.equipment)
async def provider_equipment(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 200, phone=False):
        return
    await state.update_data(equipment=message.text)
    await state.set_state(ProviderForm.location)
    await message.answer("В каких районах работаете?")


@router.message(ProviderForm.location)
async def provider_location(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 300, phone=False):
        return
    await state.update_data(location=message.text)
    await state.set_state(ProviderForm.phone)
    await message.answer("Оставьте телефон для связи.")


@router.message(ProviderForm.phone)
async def provider_phone(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 32, phone=True):
        return
    data = await state.get_data()
    async with aiosqlite.connect(storage.DB_PATH) as db:
        await db.execute(
            """INSERT INTO providers(telegram_id, name, equipment, location, phone, created_at)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(telegram_id) DO UPDATE SET
               name=excluded.name, equipment=excluded.equipment, location=excluded.location, phone=excluded.phone""",
            (message.from_user.id, message.from_user.full_name, data["equipment"], data["location"], message.text, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    await state.clear()
    await message.answer("Готово. Вы зарегистрированы как исполнитель. Когда появится подходящая заявка, она будет опубликована в канале.", reply_markup=main_menu(is_admin(state.key.user_id)))


@router.message(OfferForm.price)
async def offer_price(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 100, phone=False):
        return
    await next_offer_step(message, state, OfferForm.comment, price=message.text)


@router.message(OfferForm.comment)
async def offer_comment(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 1500, phone=False):
        return
    await next_offer_step(message, state, OfferForm.phone, comment=message.text)


@router.message(OfferForm.note)
async def offer_note(message: Message, state: FSMContext) -> None:
    if await valid_input(message, 1000, phone=False):
        await next_offer_step(message, state, OfferForm.review, note=message.text.strip())


def offer_terms_text(conditions, note=None):
    terms = conditions if conditions and conditions != "Без комментария" else "не указаны"
    return f"Условия: {terms}\nКомментарий: {note or 'Без комментария'}"


async def next_offer_step(message: Message, state: FSMContext, step: State, **fields) -> None:
    if (await state.get_data()).get("offer_editing"):
        fields["offer_editing"] = False
        step = OfferForm.review
    await show_offer_step(message, state, step, **fields)


async def confirm_offer_cancellation(message: Message, state: FSMContext, current: str) -> None:
    if current == OfferForm.cancel_confirmation.state:
        await show_offer_step(message, state, OfferForm.cancel_confirmation)
    else:
        await show_offer_step(message, state, OfferForm.cancel_confirmation, offer_cancel_from=current)


async def cancel_unsent_offer(message: Message, user_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    if await storage.has_offer(data["lead_id"], user_id):
        await message.answer("Отклик уже отправлен. Его отменить нельзя.")
        await show_main_menu(message, state)
        return
    await clear_form_keyboard(message, state)
    archive = draft_slot(state, f"cancelled_offer_{uuid4().hex}")
    if isinstance(state.storage, SQLiteDraftStorage):
        await state.storage.transfer(state.key, archive.key)
    else:
        await archive.set_data(await state.get_data())
        await archive.set_state(OfferForm.cancel_confirmation)
        await state.clear()
    await message.answer("Незавершённый отклик отменён.", reply_markup=main_menu(is_admin(state.key.user_id)))


@router.callback_query(F.data.startswith("offer_step:"))
async def offer_step_action(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    message = callback.message
    current = await state.get_state()
    data = await state.get_data()
    if (
        not isinstance(message, Message)
        or current not in OfferForm.__all_states_names__
        or message.message_id != data.get("form_keyboard_id")
    ):
        await callback.answer("Этот шаг уже неактуален.")
        if isinstance(message, Message) and message.reply_markup and any(
            (button.callback_data or "").startswith("offer_step:")
            for row in message.reply_markup.inline_keyboard for button in row
        ):
            await remove_form_keyboard(message, message.message_id)
        return
    await callback.answer()
    action = callback.data.removeprefix("offer_step:")
    if action == "home":
        await show_main_menu(message, state)
    elif action == "skip" and current == OfferForm.comment.state:
        await next_offer_step(message, state, OfferForm.phone, comment="Без комментария")
    elif action == "skip" and current == OfferForm.note.state:
        await next_offer_step(message, state, OfferForm.review, note=None)
    elif action == "back":
        await back_in_form(message, state, current)
    elif action == "edit" and current == OfferForm.review.state:
        await show_offer_step(message, state, OfferForm.edit)
    elif current == OfferForm.edit.state and action in OFFER_EDIT_FIELDS:
        await show_offer_step(message, state, OFFER_EDIT_FIELDS[action][1], offer_editing=True)
    elif action == "cancel" and current == OfferForm.review.state:
        await confirm_offer_cancellation(message, state, current)
    elif action == "cancel_confirm" and current == OfferForm.cancel_confirmation.state:
        await cancel_unsent_offer(message, callback.from_user.id, state)
    elif action == "send" and current == OfferForm.review.state:
        await send_offer(message, callback.from_user, state, bot)


@router.message(OfferForm.phone, F.text == OFFER_BACK)
async def offer_phone_back(message: Message, state: FSMContext) -> None:
    await next_offer_step(message, state, OfferForm.comment)


@router.message(OfferForm.phone)
async def offer_phone(message: Message, state: FSMContext) -> None:
    contact = message.contact
    if contact and (
        message.chat.type != "private" or message.from_user is None
        or contact.user_id != message.from_user.id or message.forward_origin is not None
    ):
        await message.answer("Можно передать только свой номер. Нажмите «Поделиться номером телефона» или введите его вручную.")
        return
    raw_phone = (contact.phone_number if contact else message.text or "").strip()
    phone = raw_phone.translate(str.maketrans("", "", " ()-\u00a0\u202f"))
    if contact and phone.startswith("7"):
        phone = "+" + phone
    if len(raw_phone) > 32 or not storage.valid_text(phone, 32, phone=True):
        await message.answer("Проверьте номер и повторите ввод. Например: +79123456789 или 89123456789.")
        return
    await next_offer_step(message, state, OfferForm.review, phone="+7" + phone[-10:])


@router.message(OfferForm.review)
@router.message(OfferForm.edit)
@router.message(OfferForm.cancel_confirmation)
async def offer_review_input(message: Message) -> None:
    await message.answer("Выберите действие кнопкой под анкетой.")


async def send_offer(message: Message, provider: User, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    phone = data["phone"]
    lead = await get_lead(int(data["lead_id"]))
    if not lead or lead["status"] != "published":
        await clear_form_keyboard(message, state)
        await state.clear()
        await message.answer("Заявка уже недоступна.", reply_markup=main_menu(is_admin(provider.id)))
        return
    provider_name = provider.full_name
    username = f"@{provider.username}" if provider.username else "без username"
    offer = (
        f"🚜 Отклик на заявку №{lead['id']}\n\n"
        f"Исполнитель: {provider_name} ({username})\n"
        f"Цена: {data['price']}\n"
        f"{offer_terms_text(data['comment'], data.get('note'))}"
    )
    offer_id = await storage.save_offer(
        lead["id"], provider.id, provider_name, username,
        data["price"], data["comment"], phone, note=data.get("note"),
    )
    await clear_form_keyboard(message, state)
    await state.clear()
    if offer_id is None:
        await message.answer("Заявка закрыта или ваш отклик уже сохранён.", reply_markup=main_menu(is_admin(provider.id)))
        return
    handle = provider.username or ""
    chat_url = (
        f"https://t.me/{handle}" if re.fullmatch(r"[A-Za-z0-9_]+", handle)
        else f"tg://user?id={provider.id}"
    )
    contacts = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Написать исполнителю", url=chat_url)],
        [InlineKeyboardButton(text="📞 Показать номер", callback_data=f"offer_call:{offer_id}")],
    ])
    delivered = await notify(
        bot, lead["customer_id"], "По вашей заявке появился отклик:\n\n" + offer,
        reply_markup=contacts,
    )
    await storage.mark_offer(offer_id, "sent" if delivered else "unconfirmed")
    for admin_id in await admin_recipients("moderate"):
        await notify(bot, admin_id, offer + f"\nТелефон: {phone}" + (
            "" if delivered else "\n⚠️ Доставка заказчику не подтверждена. Отклик сохранён."
        ))
    await message.answer(
        "✅ Отклик сохранён и отправлен заказчику." if delivered else
        "Отклик сохранён. Доставка заказчику не подтверждена, требуется проверка диспетчера.",
        reply_markup=main_menu(is_admin(provider.id)),
    )


@router.callback_query(F.data.startswith("offer_call:"))
async def show_provider_phone(callback: CallbackQuery, bot: Bot) -> None:
    offer_id = storage.parse_id(callback.data, "offer_call:")
    phone = await storage.offer_phone_for_customer(offer_id, callback.from_user.id) if offer_id else None
    if not phone:
        await callback.answer("Контакт недоступен", show_alert=True)
        return
    await callback.answer()
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Скопировать номер", copy_text=CopyTextButton(text=phone)),
    ]]) if len(phone) <= 256 else None
    await notify(bot, callback.from_user.id, f"Телефон исполнителя: {phone}", reply_markup=markup)


async def admin_lead_id(callback: CallbackQuery, prefix: str):
    if not await admin_allowed(callback.from_user.id, "moderate"):
        await callback.answer("Нет доступа", show_alert=True)
        return None
    lead_id = storage.parse_id(callback.data, prefix)
    if lead_id is None:
        await callback.answer("Некорректная заявка", show_alert=True)
    return lead_id


async def notify_test_providers(bot: Bot, lead: aiosqlite.Row) -> None:
    if not settings.test_notification_provider_ids:
        return
    routing = await lead_routing.get(lead["id"], storage.DB_PATH)
    if not routing:
        return
    recipients = await provider_dispatch.recipients_for_lead(
        **routing, db_path=storage.DB_PATH,
    )
    text = provider_dispatch.notification_text(
        lead_id=lead["id"], **routing, work_date=lead["work_date"],
        details=lead["details"],
    )
    for provider_id in recipients:
        if provider_id not in settings.test_notification_provider_ids:
            continue
        still_eligible = await provider_dispatch.recipients_for_lead(**routing, db_path=storage.DB_PATH)
        if provider_id not in still_eligible or not await notification_reservations.reserve(
            lead["id"], provider_id, storage.DB_PATH,
        ):
            continue
        delivered = await notify(bot, provider_id, public_text(text),
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[respond_button(lead["id"])]]))
        await notification_reservations.record_outcome(
            lead["id"], provider_id, "confirmed" if delivered else "uncertain", storage.DB_PATH,
        )


@router.callback_query(F.data.startswith("publish:"))
async def publish_lead(callback: CallbackQuery, bot: Bot) -> None:
    lead_id = await admin_lead_id(callback, "publish:")
    if lead_id is None:
        return
    if not await storage.transition(lead_id, "new", "publishing"):
        await callback.answer("Заявка уже обработана или ожидает проверки", show_alert=True)
        return
    try:
        await callback.answer("Публикуем заявку")
    except TelegramAPIError:
        logging.warning("Cannot acknowledge publication callback for lead %s", lead_id)
    lead = await get_lead(lead_id)
    markup = InlineKeyboardMarkup(inline_keyboard=[[respond_button(lead_id)]])
    try:
        sent = await bot.send_message(settings.leads_channel_id, lead_text(lead), reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError):
        # Explicit rejection: Telegram did not accept the message, retry is safe.
        await storage.transition(lead_id, "publishing", "new")
        await notify(bot, callback.from_user.id, "Telegram отклонил публикацию. Проверьте права и повторите.")
        return
    except TelegramAPIError:
        # Timeout/network failure may happen AFTER Telegram accepted the message.
        # Keep publishing across restarts; never retry blindly and duplicate a lead.
        await notify(bot, callback.from_user.id,
                     "Результат публикации неизвестен. Проверьте канал; автоматический повтор заблокирован.")
        return
    await storage.finish_publication(lead_id, sent.chat.id, sent.message_id)
    await admin_access.record(callback.from_user.id, "lead_publish", target_id=lead_id)
    await moderation_history.record_approval(lead['customer_id'], lead_id, storage.DB_PATH)
    await notify(bot, lead["customer_id"], f"Ваша заявка №{lead_id} опубликована.")
    await notify_test_providers(bot, lead)
    if callback.message:
        try:
            await callback.message.edit_text("✅ Заявка опубликована.\n\n" + lead_text(lead, True))
        except TelegramAPIError:
            logging.warning("Cannot update moderation message for lead %s", lead_id)


@router.callback_query(F.data.startswith("reject:"))
async def reject_lead(callback: CallbackQuery, bot: Bot) -> None:
    lead_id = await admin_lead_id(callback, "reject:")
    if lead_id is None:
        return
    if not await storage.transition(lead_id, "new", "rejected"):
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    try:
        await callback.answer("Заявка отклонена")
    except TelegramAPIError:
        logging.warning("Cannot acknowledge rejection callback for lead %s", lead_id)
    await admin_access.record(callback.from_user.id, "lead_reject", target_id=lead_id)
    lead = await get_lead(lead_id)
    await notify(bot, lead["customer_id"], "К сожалению, сейчас не сможем подобрать исполнителя по этой заявке.")
    if callback.message:
        try:
            await callback.message.edit_text("❌ Заявка отклонена.")
        except TelegramAPIError:
            logging.warning("Cannot update moderation message for lead %s", lead_id)


async def track_private_visitor(handler, event, data):
    message = event.message if isinstance(event, CallbackQuery) else event
    user = event.from_user
    if isinstance(message, Message) and message.chat.type == "private" and user and not user.is_bot:
        try:
            await storage.record_visitor(user.id)
        except aiosqlite.Error:
            logging.warning("Could not record private bot visit")
    return await handler(event, data)


def create_dispatcher(fsm_storage: SQLiteDraftStorage | None = None) -> Dispatcher:
    dispatcher = Dispatcher(
        storage=fsm_storage or MemoryStorage(), events_isolation=SimpleEventIsolation(),
        support_admin_ids=settings.admin_ids, support_group_id=settings.support_group_id,
        service_owner_id=settings.owner_id,
    )
    dispatcher.message.outer_middleware(track_private_visitor)
    dispatcher.callback_query.outer_middleware(track_private_visitor)
    dispatcher.include_router(router)
    dispatcher.include_router(provider_ui.router)
    dispatcher.include_router(support_ui.router)
    dispatcher.include_router(admin_ui.router)
    support_ui.configure_delivery_worker(dispatcher)
    return dispatcher


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=settings.token)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="menu", description="Открыть меню"),
        BotCommand(command="cancel", description="Отменить действие"),
        BotCommand(command="support", description="Помощь и поддержка"),
    ])
    await create_dispatcher().start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
