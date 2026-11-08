import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from dotenv import load_dotenv

import storage
import navigation as nav
from navigation import main_menu
import provider_registry
import provider_ui
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
router.message.filter(F.chat.type == "private")


class LeadForm(StatesGroup):
    equipment = State()
    waste_type = State()
    region = State()
    area = State()
    place = State()
    date = State()
    details = State()
    payment = State()
    phone = State()


class OfferForm(StatesGroup):
    price = State()
    comment = State()
    phone = State()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


WASTE_REQUEST = "♻️ Вывоз грунта и стройотходов"
NO_COMMENT = "Без комментария"
OFFER_PHONE_PROMPT = (
    "Введите свой телефон, чтобы заказчик мог с вами связаться. "
    "Например, +7 912 345-67-89, 79123456789 или 89123456789."
)


LEAD_STEPS = nav.Form({
    LeadForm.equipment: (
        "Выберите технику или вывоз отходов.",
        (WASTE_REQUEST,),
        *((item,) for item in sorted(provider_registry.CATEGORIES)),
    ),
    LeadForm.waste_type: (
        "Что нужно вывезти?",
        *((category,) for category in lead_routing.WASTE_CATEGORIES),
    ),
    LeadForm.region: ("Выберите регион:", ("Москва", "Московская область")),
    LeadForm.area: ("Укажите район или населённый пункт (например, СЗАО или Химки).",),
    LeadForm.place: ("Укажите точный адрес, ориентир или ближайшее метро.",),
    LeadForm.date: ("На какую дату и время нужна техника?",),
    LeadForm.details: ("Опишите работу: объём, срок, груз/навесное оборудование, важные условия.",),
    LeadForm.payment: (
        "Выберите форму оплаты:",
        (payment_preferences.CASH,), (payment_preferences.VAT,),
        (payment_preferences.NO_VAT,), (payment_preferences.ANY,),
    ),
    LeadForm.phone: (
        "Введите номер телефона для связи без пробелов, скобок и дефисов. "
        "Например, +79123456789 или 89123456789.",
    ),
})

OFFER_STEPS = nav.Form({
    OfferForm.price: ("Укажите вашу цену по заявке. Например: 28 000 ₽ за смену.",),
    OfferForm.comment: (
        "Коротко опишите условия: какая техника, подача, что включено. "
        "Если добавить нечего, нажмите «Без комментария».",
        (NO_COMMENT,),
    ),
    OfferForm.phone: (OFFER_PHONE_PROMPT,),
})

FORMS = (LEAD_STEPS, OFFER_STEPS, provider_ui.REGISTRATION_STEPS, provider_ui.MACHINE_STEPS)

LEAD_STATUSES = {
    "new": "На проверке",
    "publishing": "Публикация проверяется",
    "published": "Опубликована",
    "rejected": "Отклонена",
    "closed": "Закрыта",
}


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
    kind = "Услуга" if lead["equipment"] in lead_routing.WASTE_CATEGORIES else "Техника"
    text = (
        f"📌 Заявка №{lead['id']}\n"
        f"{kind}: {lead['equipment']}\n"
        f"Место: {lead['place']}\n"
        f"Когда: {lead['work_date']}\n"
        f"Детали: {lead['details']}"
    )
    fields = lead.keys()
    if "region" in fields and lead["region"]:
        text += f"\nРегион: {lead['region']}, {lead['area']}"
    if "payment_method" in fields and lead["payment_method"]:
        text += f"\nРасчёт за работу: {lead['payment_method']}"
    if with_phone:
        text += f"\nКонтакт заказчика: {lead['phone']}"
    return text


async def get_lead(lead_id: int):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT leads.*, lead_routing.region, lead_routing.area,
                      lead_payment_preferences.method AS payment_method
               FROM leads
               LEFT JOIN lead_routing ON lead_routing.lead_id = leads.id
               LEFT JOIN lead_payment_preferences ON lead_payment_preferences.lead_id = leads.id
               WHERE leads.id = ?""", (lead_id,),
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


async def finish_navigation(message: Message, state: FSMContext, text: str) -> None:
    current = await state.get_state()
    if any(form.contains(current) for form in (provider_ui.REGISTRATION_STEPS, provider_ui.MACHINE_STEPS)):
        await nav.return_to_provider_menu(state)
        await message.answer(text, reply_markup=nav.PROVIDER_MENU)
        return
    history = (await state.get_data()).get("screens", [])
    if history:
        await state.set_state(None)
        await message.answer(text)
        await show_section(message, state, history[-1])
        return
    await state.clear()
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await finish_navigation(message, state, "Заполнение отменено.")


@router.message(Command("menu"))
@router.message(F.text.in_({nav.HOME, "Главное меню"}))
async def home(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Главное меню", reply_markup=main_menu())


@router.message(F.text.in_({nav.BACK, "Назад"}))
async def back(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    for form in FORMS:
        if form.contains(current):
            previous = form.previous(current)
            if current == LeadForm.region.state:
                equipment = (await state.get_data()).get("equipment")
                if equipment not in lead_routing.WASTE_CATEGORIES:
                    previous = LeadForm.equipment
            if previous is None:
                await finish_navigation(message, state, "Заполнение отменено.")
            elif previous == LeadForm.details:
                await show_lead_details(message, state)
            else:
                await form.show(message, state, previous)
            return
    history = (await state.get_data()).get("screens", [])
    if len(history) > 1:
        await state.set_data({"screens": history[:-1]})
        await show_section(message, state, history[-2])
    else:
        await home(message, state)


@router.message(
    StateFilter(*(step for form in FORMS for step in form.steps)),
    F.text.in_({
        nav.PLACE_REQUEST, "📝 Оставить заявку", nav.FIND_EQUIPMENT,
        nav.PROVIDERS, "🚜 Я исполнитель", nav.MY_REQUESTS, nav.ACCOUNT, nav.HELP,
        nav.REGISTER, nav.PROFILE, nav.PAYMENT, nav.ADD_MACHINE, nav.MY_MACHINES,
        nav.ENABLE_NOTIFICATIONS, nav.DISABLE_NOTIFICATIONS,
    }),
)
async def keep_active_form(message: Message) -> None:
    await message.answer("Анкета ещё открыта. Продолжите её или нажмите «Главное меню», чтобы выйти.")


@router.message(F.text.in_({nav.PROVIDERS, "🚜 Я исполнитель"}))
async def providers_menu(message: Message, state: FSMContext) -> None:
    await nav.remember_screen(state, nav.PROVIDERS)
    await message.answer(
        "Раздел исполнителя. Здесь можно зарегистрироваться, добавить технику "
        "или открыть существующий профиль.",
        reply_markup=nav.PROVIDER_MENU,
    )


@router.message(F.text == nav.ACCOUNT)
async def account_menu(message: Message, state: FSMContext) -> None:
    await nav.remember_screen(state, nav.ACCOUNT)
    await message.answer("Личный кабинет", reply_markup=nav.ACCOUNT_MENU)


@router.message(F.text == nav.FIND_EQUIPMENT)
async def find_equipment(message: Message, state: FSMContext) -> None:
    await nav.remember_screen(state, nav.FIND_EQUIPMENT)
    await message.answer(
        "Поиск по каталогу готовится. Уже можно разместить заявку на нужную технику.",
        reply_markup=nav.section_menu((nav.PLACE_REQUEST,)),
    )


@router.message(F.text == nav.HELP)
async def help_menu(message: Message, state: FSMContext) -> None:
    await nav.remember_screen(state, nav.HELP)
    await message.answer(
        "Чтобы заказать технику, нажмите «Разместить заявку». "
        "Статус можно посмотреть в «Моих заявках». "
        "Чтобы предложить свою технику, откройте «Исполнителям». "
        "Ответы на опубликованные заявки приходят сюда. "
        "Канал заявок и связь с его администрацией: @zayavki_specteh_msk.",
        reply_markup=nav.section_menu((nav.MY_REQUESTS, nav.PROVIDERS)),
    )


@router.message(F.text == nav.MY_REQUESTS)
async def my_requests(message: Message, state: FSMContext) -> None:
    await nav.remember_screen(state, nav.MY_REQUESTS)
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            "SELECT id, equipment, work_date, status FROM leads "
            "WHERE customer_id=? ORDER BY id DESC LIMIT 10",
            (message.from_user.id,),
        ) as cursor:
            rows = await cursor.fetchall()
    if rows:
        lines = [
            f"№{lead_id} · {equipment[:120]} · {work_date[:100]} · "
            f"{LEAD_STATUSES.get(status, 'Статус уточняется')}"
            for lead_id, equipment, work_date, status in rows
        ]
        text = "Ваши заявки (до 10 последних):\n\n" + "\n".join(lines)
    else:
        text = "У вас пока нет заявок."
    await message.answer(text, reply_markup=nav.section_menu((nav.PLACE_REQUEST,)))


async def show_section(message: Message, state: FSMContext, section: str) -> None:
    handlers = {
        nav.PROVIDERS: providers_menu,
        nav.ACCOUNT: account_menu,
        nav.FIND_EQUIPMENT: find_equipment,
        nav.HELP: help_menu,
        nav.MY_REQUESTS: my_requests,
        nav.PROFILE: provider_ui.my_profile,
        nav.MY_MACHINES: provider_ui.my_machines,
        nav.PAYMENT: provider_ui.my_payment_methods,
    }
    await handlers[section](message, state)


@router.message(CommandStart(deep_link=True))
async def start_offer(message: Message, state: FSMContext, command: CommandObject) -> None:
    if await state.get_state():
        await keep_active_form(message)
        return
    await state.clear()
    if not command.args or not command.args.startswith("lead_"):
        await message.answer("Ссылка на заявку устарела. Откройте актуальную заявку в канале.", reply_markup=main_menu())
        return
    lead_id = storage.parse_id(command.args, "lead_")
    if lead_id is None:
        await message.answer("Некорректная ссылка на заявку.", reply_markup=main_menu())
        return
    lead = await get_lead(lead_id)
    if not lead or lead["status"] != "published":
        await message.answer("Эта заявка уже недоступна.", reply_markup=main_menu())
        return
    if lead["customer_id"] == message.from_user.id:
        await message.answer("На свою заявку откликнуться нельзя.", reply_markup=main_menu())
        return
    if await storage.has_offer(lead_id, message.from_user.id):
        await message.answer("Вы уже откликнулись на эту заявку.", reply_markup=main_menu())
        return
    await state.update_data(lead_id=lead_id)
    await OFFER_STEPS.show(message, state, OfferForm.price)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await keep_active_form(message)
        return
    await state.clear()
    await message.answer(
        "МСК Тех — бесплатная бета. Здесь можно оставить заявку на аренду техники "
        "или вывоз грунта и стройотходов в Москве и Московской области.",
        reply_markup=main_menu(),
    )


@router.message(F.text.in_({nav.PLACE_REQUEST, "📝 Оставить заявку"}))
async def lead_start(message: Message, state: FSMContext) -> None:
    await LEAD_STEPS.show(message, state, LeadForm.equipment)


@router.message(LeadForm.equipment)
async def lead_equipment(message: Message, state: FSMContext) -> None:
    if message.text == WASTE_REQUEST:
        await LEAD_STEPS.show(message, state, LeadForm.waste_type)
        return
    if message.text not in provider_registry.CATEGORIES:
        await LEAD_STEPS.show(message, state, LeadForm.equipment)
        return
    await state.update_data(equipment=message.text)
    await LEAD_STEPS.show(message, state, LeadForm.region)


@router.message(LeadForm.waste_type)
async def lead_waste_type(message: Message, state: FSMContext) -> None:
    if message.text not in lead_routing.WASTE_CATEGORIES:
        await LEAD_STEPS.show(message, state, LeadForm.waste_type)
        return
    await state.update_data(equipment=message.text)
    await LEAD_STEPS.show(message, state, LeadForm.region)


@router.message(LeadForm.region)
async def lead_region(message: Message, state: FSMContext) -> None:
    if message.text not in provider_registry.REGIONS:
        await message.answer("Выберите Москву или Московскую область.")
        return
    await state.update_data(region=message.text)
    await LEAD_STEPS.show(message, state, LeadForm.area)


@router.message(LeadForm.area)
async def lead_area(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 120):
        return
    await state.update_data(area=message.text.strip())
    await LEAD_STEPS.show(message, state, LeadForm.place)


@router.message(LeadForm.place)
async def lead_place(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 300, phone=False):
        return
    await state.update_data(place=message.text)
    await LEAD_STEPS.show(message, state, LeadForm.date)


@router.message(LeadForm.date)
async def lead_date(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 100, phone=False):
        return
    await state.update_data(work_date=message.text)
    await show_lead_details(message, state)


async def show_lead_details(message: Message, state: FSMContext) -> None:
    if (await state.get_data()).get("equipment") in lead_routing.WASTE_CATEGORIES:
        await state.set_state(LeadForm.details)
        await message.answer(
            "Укажите объём в м³ или тоннах, нужна ли погрузка и есть ли согласованное место приёма.",
            reply_markup=nav.section_menu(),
        )
        return
    await LEAD_STEPS.show(message, state, LeadForm.details)


@router.message(LeadForm.details)
async def lead_details(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 1500, phone=False):
        return
    await state.update_data(details=message.text)
    await LEAD_STEPS.show(message, state, LeadForm.payment)


@router.message(LeadForm.payment)
async def lead_payment(message: Message, state: FSMContext) -> None:
    if not message.text or message.text not in payment_preferences.METHODS | {payment_preferences.ANY}:
        await message.answer("Выберите форму оплаты кнопкой из списка.")
        return
    await state.update_data(payment_method=message.text)
    await LEAD_STEPS.show(message, state, LeadForm.phone)


@router.message(LeadForm.phone)
async def lead_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await valid_input(message, 32, phone=True):
        return
    data = await state.get_data()
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        async with aiosqlite.connect(storage.DB_PATH) as db:
            cursor = await db.execute(
                """INSERT INTO leads(customer_id, customer_name, equipment, place, work_date, details, phone, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message.from_user.id, message.from_user.full_name, data["equipment"], data["place"],
                 data["work_date"], data["details"], message.text.strip(), created_at),
            )
            lead_id = cursor.lastrowid
            await payment_preferences.write_lead(db, lead_id, data.get("payment_method", payment_preferences.ANY))
            await lead_routing.write(db, lead_id, data["equipment"], data["region"], data["area"])
            await db.commit()
    except aiosqlite.Error:
        logging.exception("Could not save lead and its routing")
        await message.answer("Не удалось сохранить заявку. Анкета открыта. Повторите отправку номера чуть позже.")
        return

    await state.clear()
    lead = {
        "id": lead_id, "equipment": data["equipment"], "place": data["place"],
        "work_date": data["work_date"], "details": data["details"], "phone": message.text.strip(),
        "region": data["region"], "area": data["area"], "payment_method": data.get("payment_method"),
    }
    # Diagnostics are optional. A saved lead must reach moderation even if they fail.
    try:
        shadow = await lead_shadow_review.evaluate(
            message.from_user.id, equipment=data["equipment"],
            region=data["region"], area=data["area"], place=data["place"],
            work_date=data["work_date"], details=data["details"],
            db_path=storage.DB_PATH, enabled=False, exclude_lead_id=lead_id,
        )
        logging.info("Lead %s shadow route: %s", lead_id, shadow.route)
    except aiosqlite.Error:
        logging.exception("Shadow review unavailable for lead %s", lead_id)
    markup = InlineKeyboardMarkup(inline_keyboard=[[ 
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{lead_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{lead_id}"),
    ]])
    for admin_id in settings.admin_ids:
        await notify(bot, admin_id, "Новая заявка на проверку:\n\n" + lead_text(lead, True), reply_markup=markup)
    await message.answer("Спасибо! Проверим заявку и передадим её подходящим исполнителям.", reply_markup=main_menu())


@router.message(OfferForm.price)
async def offer_price(message: Message, state: FSMContext) -> None:
    if not storage.valid_text(message.text, 100):
        await OFFER_STEPS.show(message, state, OfferForm.price)
        return
    await state.update_data(price=message.text)
    await OFFER_STEPS.show(message, state, OfferForm.comment)


@router.message(OfferForm.comment)
async def offer_comment(message: Message, state: FSMContext) -> None:
    if not storage.valid_text(message.text, 1500):
        await OFFER_STEPS.show(message, state, OfferForm.comment)
        return
    await state.update_data(comment=message.text.strip())
    await OFFER_STEPS.show(message, state, OfferForm.phone)


@router.message(OfferForm.phone)
async def offer_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    raw_phone = (message.text or "").strip()
    phone = raw_phone.translate(str.maketrans("", "", " ()-\u00a0\u202f"))
    if phone.startswith("7"):
        phone = "+" + phone
    if len(raw_phone) > 32 or not storage.valid_text(phone, 32, phone=True):
        await OFFER_STEPS.show(message, state, OfferForm.phone)
        return
    phone = "+7" + phone[-10:]
    data = await state.get_data()
    lead = await get_lead(int(data["lead_id"]))
    if not lead or lead["status"] != "published":
        await state.clear()
        await message.answer("Заявка уже недоступна.", reply_markup=main_menu())
        return
    provider_name = message.from_user.full_name
    username = f"@{message.from_user.username}" if message.from_user.username else "без username"
    offer = (
        f"🚜 Отклик на заявку №{lead['id']}\n\n"
        f"Исполнитель: {provider_name} ({username})\n"
        f"Цена: {data['price']}\n"
        f"Условия: {data['comment']}\n"
        f"Телефон: {phone}"
    )
    offer_id = await storage.save_offer(
        lead["id"], message.from_user.id, provider_name, username,
        data["price"], data["comment"], phone,
    )
    await state.clear()
    if offer_id is None:
        duplicate = await storage.has_offer(lead["id"], message.from_user.id)
        text = "Вы уже откликнулись на эту заявку." if duplicate else "Заявка уже недоступна."
        await message.answer(text, reply_markup=main_menu())
        return
    delivered = await notify(bot, lead["customer_id"], "По вашей заявке появился отклик:\n\n" + offer)
    await storage.mark_offer(offer_id, "sent" if delivered else "unconfirmed")
    for admin_id in settings.admin_ids:
        await notify(bot, admin_id, offer + ("" if delivered else "\n⚠️ Доставка заказчику не подтверждена. Отклик сохранён."))
    await message.answer(
        "✅ Отклик сохранён и отправлен заказчику." if delivered else
        "Отклик сохранён. Доставка заказчику не подтверждена, требуется проверка диспетчера.",
        reply_markup=main_menu(),
    )


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
    if not recipients:
        return
    text = provider_dispatch.notification_text(
        lead_id=lead["id"], **routing, work_date=lead["work_date"],
        details=lead["details"], bot_username=settings.bot_username,
    )
    for provider_id in recipients:
        if provider_id not in settings.test_notification_provider_ids:
            continue
        if not await notification_reservations.reserve(lead["id"], provider_id, storage.DB_PATH):
            continue
        delivered = await notify(bot, provider_id, text)
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
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🚜 Откликнуться",
        url=f"https://t.me/{settings.bot_username}?start=lead_{lead_id}",
    )]])
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


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(router)
    dp.include_router(provider_ui.router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=settings.token)
    await create_dispatcher().start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
