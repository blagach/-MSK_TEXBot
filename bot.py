import asyncio
import logging
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

from draft_storage import SQLiteDraftStorage, draft_slot

import storage
import provider_registry
import provider_ui
from provider_ui import AVAILABLE_LEADS, LEGACY_PROVIDER, MY_OFFERS, OFFER_DRAFT, section_keyboard
import provider_dispatch
import notification_reservations
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


def read_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "")
    admins = {int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip()}
    channel = os.getenv("LEADS_CHANNEL_ID", "")
    if not token or not admins or not channel:
        raise RuntimeError("Заполните BOT_TOKEN, ADMIN_IDS и LEADS_CHANNEL_ID в .env")
    return Settings(
        token,
        admins,
        int(channel),
        os.getenv("PAYMENT_URL", ""),
        os.getenv("BOT_USERNAME", "MSK_TEXBot").lstrip("@"),
        frozenset(int(value.strip()) for value in os.getenv("TEST_NOTIFICATION_PROVIDER_IDS", "").split(",") if value.strip()),
    )


settings = read_settings()
router = Router()


class LeadForm(StatesGroup):
    equipment = State()
    region = State()
    area = State()
    place = State()
    date = State()
    details = State()
    payment = State()
    phone = State()


class ProviderForm(StatesGroup):
    equipment = State()
    location = State()
    phone = State()


class OfferForm(StatesGroup):
    price = State()
    comment = State()
    phone = State()
    review = State()
    edit = State()
    cancel_confirmation = State()


OFFER_PROMPTS = {
    OfferForm.price: "Укажите вашу цену по заявке. Например: 28 000 ₽ за смену.",
    OfferForm.comment: "Коротко опишите условия: какая техника, подача, что включено.",
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
    LeadForm.region: "Выберите регион:",
    LeadForm.area: "Укажите район или населённый пункт (например, СЗАО или Химки).",
    LeadForm.place: "Укажите точный адрес, ориентир или ближайшее метро.",
    LeadForm.date: "На какую дату и время нужна техника?",
    LeadForm.details: "Опишите работу: объём, срок, груз/навесное оборудование, важные условия.",
    LeadForm.payment: "Выберите форму оплаты:",
    LeadForm.phone: (
        "Введите номер телефона для связи без пробелов, скобок и дефисов. "
        "Например, +79123456789 или 89123456789."
    ),
}
LEAD_CHOICES = {
    LeadForm.equipment: (tuple(sorted(provider_registry.CATEGORIES)), "equipment", LeadForm.region),
    LeadForm.region: (("Москва", "Московская область"), "region", LeadForm.area),
    LeadForm.payment: (
        (payment_preferences.CASH, payment_preferences.VAT, payment_preferences.NO_VAT, payment_preferences.ANY),
        "payment_method", LeadForm.phone,
    ),
}
FORM_STEPS = {step.state: step for step in (*LeadForm.__all_states__, *OfferForm.__all_states__)}
LEAD_DRAFT = "▶️ Продолжить заявку"
CUSTOMER_ROLE = "📋 Я заказчик"
PROVIDER_ROLE = "🚜 Я исполнитель"
MY_LEADS = "📋 Мои заявки"
OFFER_EDIT_FIELDS = {
    "price": ("💰 Стоимость", OfferForm.price),
    "comment": ("📝 Комментарий", OfferForm.comment),
    "phone": ("📱 Телефон", OfferForm.phone),
}


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def main_menu() -> ReplyKeyboardMarkup:
    return section_keyboard((CUSTOMER_ROLE, PROVIDER_ROLE))


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
    await storage.init_schema()
    await provider_registry.init_schema(storage.DB_PATH)
    await lead_routing.init_schema(storage.DB_PATH)
    await moderation_history.init_schema(storage.DB_PATH)
    await payment_preferences.init_schema(storage.DB_PATH)
    await notification_reservations.init_schema(storage.DB_PATH)


def lead_text(lead: aiosqlite.Row, with_phone: bool = False) -> str:
    text = (
        f"📌 Заявка №{lead['id']}\n"
        f"Техника: {lead['equipment']}\n"
        f"Место: {lead['place']}\n"
        f"Когда: {lead['work_date']}\n"
        f"Детали: {lead['details'] or 'не указаны'}"
    )
    if with_phone:
        text += f"\nКонтакт заказчика: {lead['phone']}"
    return text


async def get_lead(lead_id: int):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)) as cursor:
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
    options = LEAD_CHOICES[step][0] if step in LEAD_CHOICES else ()
    rows = [[InlineKeyboardButton(text=label, callback_data=f"lead_step:{index}")]
            for index, label in enumerate(options)]
    if step == LeadForm.details:
        rows.append([InlineKeyboardButton(text="Пропустить описание", callback_data="lead_skip_details")])
    navigation = []
    if step != LeadForm.equipment:
        navigation.append(InlineKeyboardButton(text=OFFER_BACK, callback_data="form_nav:back"))
    navigation.append(InlineKeyboardButton(text=OFFER_HOME, callback_data="form_nav:home"))
    rows.append(navigation)
    await send_form_panel(message, state, LEAD_PROMPTS[step], rows)


def form_kind(current: str | None) -> str | None:
    if current in LeadForm.__all_states_names__:
        return "lead"
    if current in OfferForm.__all_states_names__:
        return "offer"
    return None


async def pause_form(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    kind = form_kind(current)
    await clear_form_keyboard(message, state)
    if kind:
        draft = draft_slot(state, kind)
        if isinstance(state.storage, SQLiteDraftStorage):
            await state.storage.transfer(state.key, draft.key)
            return
        data = await state.get_data()
        data.pop("form_keyboard_id", None)
        await draft.set_data(data)
        await draft.set_state(current)
    await state.clear()


async def restore_form_step(message: Message, state: FSMContext, current: str) -> None:
    step = FORM_STEPS[current]
    if form_kind(current) == "lead":
        await show_lead_step(message, state, step)
    else:
        await show_offer_step(message, state, step)


async def resume_draft(message: Message, state: FSMContext, kind: str) -> bool:
    draft = draft_slot(state, kind)
    current = await draft.get_state()
    if current is None:
        return False
    if form_kind(await state.get_state()) == kind:
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
        steps = LeadForm.__all_states__
        index = steps.index(FORM_STEPS[current])
        await show_lead_step(message, state, steps[max(0, index - 1)])
    elif current == OfferForm.phone.state:
        await offer_phone_back(message, state)
    elif current == OfferForm.comment.state and not (await state.get_data()).get("offer_editing"):
        await show_offer_step(message, state, OfferForm.price)
    elif current in (OfferForm.price.state, OfferForm.comment.state) and (await state.get_data()).get("offer_editing"):
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
    if not await resume_draft(message, state, "lead" if message.text == LEAD_DRAFT else "offer"):
        await open_navigation(message, state)


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
            f"📝 Комментарий: {data['comment']}\n"
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
            if step == OfferForm.comment:
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
    await message.answer("Заполнение отменено.", reply_markup=main_menu())


@router.message(CommandStart(deep_link=True))
async def start_offer(message: Message, state: FSMContext, command: CommandObject) -> None:
    lead_id = storage.parse_id(command.args, "lead_")
    lead = await get_lead(lead_id) if lead_id else None
    if not lead or lead["status"] != "published":
        await message.answer("Ссылка на заявку устарела или заявка недоступна.")
        await open_navigation(message, state)
        return
    if await storage.has_offer(lead_id, message.from_user.id):
        await message.answer(f"Вы уже откликнулись на заявку №{lead_id}.")
        return
    current = await state.get_state()
    if form_kind(current) == "offer":
        if (await state.get_data()).get("lead_id") == lead_id:
            await restore_form_step(message, state, current)
        else:
            await message.answer("У вас открыт другой отклик. Продолжите его или отмените командой /cancel.")
            await open_navigation(message, state)
        return
    saved = draft_slot(state, "offer")
    if await saved.get_state():
        if (await saved.get_data()).get("lead_id") != lead_id:
            await message.answer("У вас есть черновик другого отклика. Сначала завершите его или отмените командой /cancel.")
        await resume_draft(message, state, "offer")
        return
    await pause_form(message, state)
    await message.answer(f"Отклик на заявку №{lead_id}.", reply_markup=ReplyKeyboardRemove())
    await show_offer_step(message, state, OfferForm.price, lead_id=lead_id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await message.answer(
        "Здравствуйте! Здесь можно быстро оставить заявку на аренду строительной или дорожной техники в Москве и МО.",
        reply_markup=main_menu(),
    )


@router.message(F.text == OFFER_HOME)
async def show_main_menu(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    await message.answer("Главное меню MSK_TEX. Выберите нужный раздел:",
                         reply_markup=main_menu())


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


@router.message(F.text == MY_LEADS)
async def my_leads(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            "SELECT id, equipment, status FROM leads WHERE customer_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,),
        ) as cursor:
            leads = await cursor.fetchall()
    statuses = {"new": "на проверке", "publishing": "публикуется", "published": "опубликована",
                "rejected": "отклонена"}
    lines = [f"№{lead_id} · {equipment} · {statuses.get(status, status)}" for lead_id, equipment, status in leads]
    await message.answer("Ваши последние заявки:\n" + "\n".join(lines) if lines else "У вас пока нет заявок.")


@router.message(F.text.in_({AVAILABLE_LEADS, "🚜 Смотреть заявки"}))
async def available_leads(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            """SELECT id, equipment, place, work_date FROM leads WHERE status='published'
               ORDER BY id DESC LIMIT 10"""
        ) as cursor:
            leads = await cursor.fetchall()
    if not leads:
        await message.answer("Опубликованных заявок пока нет.")
        return
    for lead_id, equipment, place, date in leads:
        await message.answer(
            f"Заявка №{lead_id} · {equipment}\n{place} · {date}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[respond_button(lead_id)]]),
        )


@router.message(F.text == MY_OFFERS)
async def my_offers(message: Message, state: FSMContext) -> None:
    await pause_form(message, state)
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            "SELECT lead_id, price, delivery_status FROM offers WHERE provider_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,),
        ) as cursor:
            offers = await cursor.fetchall()
    statuses = {"sent": "отправлен", "pending": "доставка ожидается", "unconfirmed": "доставка не подтверждена"}
    lines = [f"Заявка №{lead_id} · {price} · {statuses.get(status, status)}" for lead_id, price, status in offers]
    await message.answer("Ваши последние отклики:\n" + "\n".join(lines) if lines else "У вас пока нет откликов.")


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


async def apply_lead_choice(message: Message, state: FSMContext, step: State, choice: str) -> None:
    _, field, next_step = LEAD_CHOICES[step]
    await show_lead_step(message, state, next_step, **{field: choice})


@router.callback_query(F.data.startswith("lead_step:"))
async def lead_choice(callback: CallbackQuery, state: FSMContext) -> None:
    step = FORM_STEPS.get(await state.get_state())
    suffix = callback.data.removeprefix("lead_step:")
    data = await state.get_data()
    if (
        step not in LEAD_CHOICES or not isinstance(callback.message, Message)
        or callback.message.message_id != data.get("form_keyboard_id")
        or not re.fullmatch(r"[0-9]{1,3}", suffix)
        or int(suffix) >= len(LEAD_CHOICES[step][0])
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    await callback.answer()
    await apply_lead_choice(callback.message, state, step, LEAD_CHOICES[step][0][int(suffix)])


@router.message(LeadForm.equipment)
async def lead_equipment(message: Message, state: FSMContext) -> None:
    if message.text not in provider_registry.CATEGORIES:
        await message.answer("Выберите категорию техники кнопкой из списка или введите /menu.")
        return
    await apply_lead_choice(message, state, LeadForm.equipment, message.text)


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
    await show_lead_step(message, state, LeadForm.place, area=message.text.strip())


@router.message(LeadForm.place)
async def lead_place(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 300, phone=False):
        return
    await show_lead_step(message, state, LeadForm.date, place=message.text)


@router.message(LeadForm.date)
async def lead_date(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 100, phone=False):
        return
    await show_lead_step(message, state, LeadForm.details, work_date=message.text)


@router.callback_query(F.data == "lead_skip_details")
async def skip_lead_details(callback: CallbackQuery, state: FSMContext) -> None:
    if (
        await state.get_state() != LeadForm.details.state or not isinstance(callback.message, Message)
        or callback.message.message_id != (await state.get_data()).get("form_keyboard_id")
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    await callback.answer()
    await show_lead_step(callback.message, state, LeadForm.payment, details="")


@router.message(LeadForm.details)
async def lead_details(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 1500, phone=False):
        return
    await show_lead_step(message, state, LeadForm.payment, details=message.text)


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
        await payment_preferences.write_lead(db, lead_id, data.get("payment_method", payment_preferences.ANY))
        await lead_routing.write_lead(db, lead_id, data["equipment"], data["region"], data["area"])
        if draft_id:
            await db.execute("INSERT INTO lead_draft_submissions VALUES (?,?)", (draft_id, lead_id))
        await db.commit()
        return lead_id, True


@router.message(LeadForm.phone)
async def lead_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await valid_input(message, 32, phone=True):
        return
    data = await state.get_data()
    lead_id, created = await save_lead_from_draft(message.from_user, message.text, data)
    if not created:
        await clear_form_keyboard(message, state)
        await state.clear()
        await message.answer(f"Заявка №{lead_id} уже сохранена.", reply_markup=main_menu())
        return
    lead = await get_lead(lead_id)
    # Shadow evaluation only: never changes the manual approval workflow.
    shadow = await lead_shadow_review.evaluate(
        message.from_user.id, equipment=data["equipment"],
        region=data["region"], area=data["area"], place=data["place"],
        work_date=data["work_date"], details=data["details"],
        db_path=storage.DB_PATH, enabled=False, exclude_lead_id=lead_id,
    )
    logging.info("Lead %s shadow route: %s", lead_id, shadow.route)
    markup = InlineKeyboardMarkup(inline_keyboard=[[ 
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{lead_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{lead_id}"),
    ]])
    for admin_id in settings.admin_ids:
        await notify(bot, admin_id, "Новая заявка на проверку:\n\n" + lead_text(lead, True), reply_markup=markup)
    await clear_form_keyboard(message, state)
    await state.clear()
    await message.answer("Спасибо! Проверим заявку и передадим её подходящим исполнителям.", reply_markup=main_menu())


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
    await message.answer("Готово. Вы зарегистрированы как исполнитель. Когда появится подходящая заявка, она будет опубликована в канале.", reply_markup=main_menu())


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
    await message.answer("Незавершённый отклик отменён.", reply_markup=main_menu())


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
        await message.answer("Заявка уже недоступна.", reply_markup=main_menu())
        return
    provider_name = provider.full_name
    username = f"@{provider.username}" if provider.username else "без username"
    offer = (
        f"🚜 Отклик на заявку №{lead['id']}\n\n"
        f"Исполнитель: {provider_name} ({username})\n"
        f"Цена: {data['price']}\n"
        f"Условия: {data['comment']}"
    )
    offer_id = await storage.save_offer(
        lead["id"], provider.id, provider_name, username,
        data["price"], data["comment"], phone,
    )
    await clear_form_keyboard(message, state)
    await state.clear()
    if offer_id is None:
        await message.answer("Заявка закрыта или ваш отклик уже сохранён.", reply_markup=main_menu())
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
    for admin_id in settings.admin_ids:
        await notify(bot, admin_id, offer + f"\nТелефон: {phone}" + (
            "" if delivered else "\n⚠️ Доставка заказчику не подтверждена. Отклик сохранён."
        ))
    await message.answer(
        "✅ Отклик сохранён и отправлен заказчику." if delivered else
        "Отклик сохранён. Доставка заказчику не подтверждена, требуется проверка диспетчера.",
        reply_markup=main_menu(),
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
    if not is_admin(callback.from_user.id):
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
        if not await notification_reservations.reserve(lead["id"], provider_id, storage.DB_PATH):
            continue
        delivered = await notify(bot, provider_id, text,
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
    await moderation_history.record_approval(lead['customer_id'], lead_id, storage.DB_PATH)
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
    lead = await get_lead(lead_id)
    await notify(bot, lead["customer_id"], "К сожалению, сейчас не сможем подобрать исполнителя по этой заявке.")
    if callback.message:
        try:
            await callback.message.edit_text("❌ Заявка отклонена.")
        except TelegramAPIError:
            logging.warning("Cannot update moderation message for lead %s", lead_id)


def create_dispatcher(fsm_storage: SQLiteDraftStorage | None = None) -> Dispatcher:
    dispatcher = Dispatcher(storage=fsm_storage or MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(router)
    dispatcher.include_router(provider_ui.router)
    return dispatcher


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=settings.token)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="menu", description="Открыть меню"),
        BotCommand(command="cancel", description="Отменить действие"),
    ])
    await create_dispatcher().start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
