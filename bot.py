import asyncio
import logging
import re
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
    KeyboardButton,
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
import waste_requests as waste

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
    region = State()
    area = State()
    place = State()
    date = State()
    details = State()
    payment = State()
    phone = State()
    waste_kind = State()
    waste_material = State()
    waste_hazard = State()
    waste_service = State()
    waste_volume = State()
    waste_mass = State()
    waste_loading = State()
    waste_destination = State()


class OfferForm(StatesGroup):
    price = State()
    comment = State()
    phone = State()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


LEAD_STEPS = nav.Form({
    LeadForm.equipment: (
        "Выберите категорию техники или услугу:",
        (waste.CATEGORY,),
        *((item,) for item in sorted(provider_registry.CATEGORIES)),
    ),
    LeadForm.region: ("Выберите регион:", ("Москва", "Московская область")),
    LeadForm.area: ("Укажите район или населённый пункт (например, СЗАО или Химки).",),
    LeadForm.place: ("Укажите точный адрес, ориентир или ближайшее метро.",),
    LeadForm.date: ("На какую дату и время нужна техника?",),
    LeadForm.details: ("Опишите работу: объём, срок, груз, навесное оборудование, важные условия.",),
    LeadForm.payment: (
        "Выберите форму оплаты:",
        (payment_preferences.CASH,), (payment_preferences.VAT,),
        (payment_preferences.NO_VAT,), (payment_preferences.ANY,),
    ),
    LeadForm.phone: (
        "Введите телефон для связи или отправьте свой контакт.",
        (KeyboardButton(text=nav.SHARE_PHONE, request_contact=True),),
    ),
})

WASTE_STEPS = nav.Form({
    LeadForm.equipment: LEAD_STEPS.steps[LeadForm.equipment],
    LeadForm.waste_kind: ("Что нужно вывезти?", *((label,) for label in waste.DIRECTIONS.values())),
    LeadForm.waste_material: ("Выберите материал.",),
    LeadForm.waste_hazard: (
        "Укажите класс опасности, если он установлен документами.",
        waste.HAZARD_CLASSES,
        (waste.UNKNOWN,),
    ),
    LeadForm.waste_service: (
        "Какая услуга нужна? Комплексный вывоз включает погрузку и передачу материала "
        "на площадку, имеющую право его принять.",
        *((label,) for label in waste.SERVICES.values()),
    ),
    LeadForm.waste_volume: ("Укажите примерный объём в м³. Например, 12,5.",),
    LeadForm.waste_mass: ("Укажите массу в тоннах, если она известна.", (waste.UNKNOWN,)),
    LeadForm.waste_loading: ("Какие условия погрузки?", waste.LOADING),
    LeadForm.region: LEAD_STEPS.steps[LeadForm.region],
    LeadForm.area: LEAD_STEPS.steps[LeadForm.area],
    LeadForm.place: ("Укажите точный адрес погрузки или ориентир.",),
    LeadForm.waste_destination: ("Укажите адрес приёма, если он уже согласован.", (waste.NO_DESTINATION,)),
    LeadForm.date: ("На какую дату и время нужен вывоз?",),
    LeadForm.details: ("Укажите требования к перевозке и другие условия.", (waste.NO_DETAILS,)),
    LeadForm.payment: LEAD_STEPS.steps[LeadForm.payment],
    LeadForm.phone: LEAD_STEPS.steps[LeadForm.phone],
})

OFFER_STEPS = nav.Form({
    OfferForm.price: ("Укажите вашу цену по заявке. Например: 28 000 ₽ за смену.",),
    OfferForm.comment: (
        "Коротко опишите условия: какая техника, подача, что включено. "
        "Если добавить нечего, нажмите «Без комментария».",
        ("Без комментария",),
    ),
    OfferForm.phone: (
        "Введите свой телефон, чтобы заказчик мог с вами связаться.",
        (KeyboardButton(text=nav.SHARE_PHONE, request_contact=True),),
        (nav.PROFILE_PHONE,),
    ),
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
    if "waste_kind" in lead.keys() and lead["waste_kind"]:
        return waste_lead_text(lead, with_phone)
    text = (
        f"📌 Заявка №{lead['id']}\n"
        f"Техника: {lead['equipment']}\n"
        f"Место: {lead['place']}\n"
        f"Когда: {lead['work_date']}\n"
        f"Детали: {lead['details']}"
    )
    payment_method = lead["payment_method"] or "Не указана"
    text += f"\nФорма оплаты: {payment_method}"
    if with_phone:
        text += f"\nКонтакт заказчика: {lead['phone']}"
    return text


def waste_lead_text(lead: aiosqlite.Row, with_phone: bool) -> str:
    mass = waste.format_amount(lead["mass_kg"]) + " т" if lead["mass_kg"] else "Не указана"
    hazard = lead["hazard_class"] or "Не указан"
    lines = [
        f"📌 Заявка №{lead['id']}",
        f"Направление: {lead['equipment']}",
        f"Услуга: {waste.SERVICES[lead['waste_service']]}",
        f"Материал: {lead['material']}",
        f"Объём: {waste.format_amount(lead['volume_liters'])} м³",
        f"Масса: {mass}",
        f"Погрузка: {lead['loading']}",
        f"Район погрузки: {lead['region']}, {lead['area']}",
        f"Когда: {lead['work_date']}",
        f"Условия: {lead['details']}",
        f"Форма оплаты: {lead['payment_method'] or 'Не указана'}",
    ]
    if lead["waste_kind"] == "construction":
        lines.append(f"Заявленный класс опасности: {hazard}")
    if with_phone:
        if lead["waste_kind"] == "construction" and lead["hazard_class"] != "V":
            lines.append("Перед публикацией уточните применимые документы и условия приёма.")
        lines.extend((
            f"Адрес погрузки: {lead['place']}",
            f"Адрес приёма: {lead['destination'] or 'Уточняется'}",
            f"Контакт заказчика: {lead['phone']}",
        ))
    return "\n".join(lines)


async def read_lead(db: aiosqlite.Connection, lead_id: int) -> aiosqlite.Row | None:
    db.row_factory = aiosqlite.Row
    async with db.execute(
        """SELECT leads.*, lead_payment_preferences.method AS payment_method,
                  waste_lead_details.kind AS waste_kind, waste_lead_details.service AS waste_service,
                  waste_lead_details.material, waste_lead_details.hazard_class,
                  waste_lead_details.volume_liters, waste_lead_details.mass_kg,
                  waste_lead_details.loading, waste_lead_details.destination,
                  lead_routing.region, lead_routing.area
           FROM leads
           LEFT JOIN lead_payment_preferences ON lead_payment_preferences.lead_id = leads.id
           LEFT JOIN waste_lead_details ON waste_lead_details.lead_id = leads.id
           LEFT JOIN lead_routing ON lead_routing.lead_id = leads.id
           WHERE leads.id = ?""",
        (lead_id,),
    ) as cursor:
        return await cursor.fetchone()


async def get_lead(lead_id: int) -> aiosqlite.Row | None:
    async with aiosqlite.connect(storage.DB_PATH) as db:
        return await read_lead(db, lead_id)


async def valid_input(message: Message, limit: int) -> bool:
    if not storage.valid_text(message.text, limit):
        await message.answer(f"Отправьте текст от 1 до {limit} символов.")
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
    if (any(form.contains(current) for form in (provider_ui.REGISTRATION_STEPS, provider_ui.MACHINE_STEPS))
            or current in provider_ui.PROFILE_STATES):
        await nav.return_to_provider_menu(state)
        await message.answer(text, reply_markup=await provider_ui.provider_keyboard(message.from_user.id))
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
    if current in provider_ui.PROFILE_STATES:
        await finish_navigation(message, state, "Заполнение отменено.")
        return
    data = await state.get_data()
    forms = (WASTE_STEPS, *FORMS[1:]) if data.get("waste_flow") else FORMS
    for form in forms:
        if form.contains(current):
            previous = form.previous(current)
            if previous == LeadForm.waste_hazard and data.get("waste_kind") != "construction":
                previous = LeadForm.waste_material
            if previous == LeadForm.waste_loading and data.get("waste_service") == "full":
                previous = LeadForm.waste_mass
            if previous is None:
                await finish_navigation(message, state, "Заполнение отменено.")
            elif previous == provider_ui.Register.payment:
                await provider_ui.show_selection(message, state, previous, payment_preferences.PAYMENT_OPTIONS)
            elif form is WASTE_STEPS:
                await show_lead_step(message, state, previous)
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
    StateFilter(*WASTE_STEPS.steps, *(step for form in FORMS[1:] for step in form.steps), *provider_ui.PROFILE_STATES),
    F.text.in_({
        nav.PLACE_REQUEST, "📝 Оставить заявку", nav.FIND_EQUIPMENT,
        nav.PROVIDERS, "🚜 Я исполнитель", nav.MY_REQUESTS, nav.ACCOUNT, nav.HELP,
        nav.REGISTER, nav.PROFILE, nav.PAYMENT, nav.ADD_MACHINE, nav.MY_MACHINES,
        nav.ENABLE_NOTIFICATIONS, nav.DISABLE_NOTIFICATIONS, nav.NOTIFICATIONS_ON, nav.NOTIFICATIONS_OFF,
        nav.EDIT_PROFILE, nav.SERVICES,
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
        reply_markup=await provider_ui.provider_keyboard(message.from_user.id),
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
    if not command.args or not command.args.startswith("lead_"):
        if await state.get_state():
            await keep_active_form(message)
            return
        await message.answer("Ссылка на заявку устарела. Откройте актуальную заявку в канале.", reply_markup=main_menu())
        return
    lead_id = storage.parse_id(command.args, "lead_")
    if lead_id is None:
        await message.answer("Некорректная ссылка на заявку.", reply_markup=main_menu())
        return
    if await storage.has_offer(lead_id, message.from_user.id):
        current = await state.get_state()
        if OFFER_STEPS.contains(current) and (await state.get_data()).get("lead_id") == lead_id:
            await state.clear()
            await message.answer("Вы уже откликнулись на эту заявку", reply_markup=main_menu())
        else:
            await message.answer("Вы уже откликнулись на эту заявку")
        return
    if await state.get_state():
        await keep_active_form(message)
        return
    await state.clear()
    lead = await get_lead(lead_id)
    if not lead or lead["status"] != "published":
        await message.answer("Эта заявка уже недоступна.", reply_markup=main_menu())
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
        "Здравствуйте! Здесь можно быстро оставить заявку на аренду строительной или дорожной техники в Москве и МО.",
        reply_markup=main_menu(),
    )


@router.message(F.text.in_({nav.PLACE_REQUEST, "📝 Оставить заявку"}))
async def lead_start(message: Message, state: FSMContext) -> None:
    await LEAD_STEPS.show(message, state, LeadForm.equipment)


async def show_lead_step(message: Message, state: FSMContext, step: State) -> None:
    data = await state.get_data()
    if step == LeadForm.waste_material:
        await state.set_state(step)
        choices = waste.MATERIALS[data["waste_kind"]]
        prompt = WASTE_STEPS.steps[step][0]
        await message.answer(prompt, reply_markup=nav.section_menu(*((label,) for label in choices)))
        return
    form = WASTE_STEPS if data.get("waste_flow") else LEAD_STEPS
    await form.show(message, state, step)


@router.message(LeadForm.equipment)
async def lead_equipment(message: Message, state: FSMContext) -> None:
    if message.text == waste.CATEGORY:
        await state.update_data(waste_flow=True)
        await show_lead_step(message, state, LeadForm.waste_kind)
        return
    if message.text not in provider_registry.CATEGORIES:
        await message.answer("Выберите категорию техники или услугу кнопкой из списка.")
        return
    await state.update_data(equipment=message.text, waste_flow=False)
    await show_lead_step(message, state, LeadForm.region)


@router.message(LeadForm.waste_kind)
async def lead_waste_kind(message: Message, state: FSMContext) -> None:
    for kind, label in waste.DIRECTIONS.items():
        if message.text == label:
            await state.update_data(waste_kind=kind, equipment=label, waste_hazard=None)
            await show_lead_step(message, state, LeadForm.waste_material)
            return
    await show_lead_step(message, state, LeadForm.waste_kind)


@router.message(LeadForm.waste_material)
async def lead_waste_material(message: Message, state: FSMContext) -> None:
    kind = (await state.get_data())["waste_kind"]
    if message.text not in waste.MATERIALS[kind]:
        await show_lead_step(message, state, LeadForm.waste_material)
        return
    await state.update_data(waste_material=message.text)
    step = LeadForm.waste_hazard if kind == "construction" else LeadForm.waste_service
    await show_lead_step(message, state, step)


@router.message(LeadForm.waste_hazard)
async def lead_waste_hazard(message: Message, state: FSMContext) -> None:
    if message.text not in (*waste.HAZARD_CLASSES, waste.UNKNOWN):
        await show_lead_step(message, state, LeadForm.waste_hazard)
        return
    await state.update_data(waste_hazard=None if message.text == waste.UNKNOWN else message.text)
    await show_lead_step(message, state, LeadForm.waste_service)


@router.message(LeadForm.waste_service)
async def lead_waste_service(message: Message, state: FSMContext) -> None:
    for service, label in waste.SERVICES.items():
        if message.text == label:
            await state.update_data(waste_service=service)
            await show_lead_step(message, state, LeadForm.waste_volume)
            return
    await show_lead_step(message, state, LeadForm.waste_service)


@router.message(LeadForm.waste_volume)
async def lead_waste_volume(message: Message, state: FSMContext) -> None:
    volume = waste.to_thousandths(message.text)
    if volume is None:
        await show_lead_step(message, state, LeadForm.waste_volume)
        return
    await state.update_data(waste_volume=volume)
    await show_lead_step(message, state, LeadForm.waste_mass)


@router.message(LeadForm.waste_mass)
async def lead_waste_mass(message: Message, state: FSMContext) -> None:
    mass = waste.to_thousandths(message.text)
    if mass is None and message.text != waste.UNKNOWN:
        await show_lead_step(message, state, LeadForm.waste_mass)
        return
    await state.update_data(waste_mass=mass)
    if (await state.get_data())["waste_service"] == "full":
        await state.update_data(waste_loading="Погрузка включена")
        await show_lead_step(message, state, LeadForm.region)
        return
    await show_lead_step(message, state, LeadForm.waste_loading)


@router.message(LeadForm.waste_loading)
async def lead_waste_loading(message: Message, state: FSMContext) -> None:
    if message.text not in waste.LOADING:
        await show_lead_step(message, state, LeadForm.waste_loading)
        return
    await state.update_data(waste_loading=message.text)
    await show_lead_step(message, state, LeadForm.region)


@router.message(LeadForm.region)
async def lead_region(message: Message, state: FSMContext) -> None:
    if message.text not in provider_registry.REGIONS:
        await message.answer("Выберите Москву или Московскую область.")
        return
    await state.update_data(region=message.text)
    await show_lead_step(message, state, LeadForm.area)


@router.message(LeadForm.area)
async def lead_area(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 120):
        return
    await state.update_data(area=message.text.strip())
    await show_lead_step(message, state, LeadForm.place)


@router.message(LeadForm.place)
async def lead_place(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 300):
        return
    await state.update_data(place=message.text)
    data = await state.get_data()
    step = LeadForm.waste_destination if data.get("waste_flow") else LeadForm.date
    await show_lead_step(message, state, step)


@router.message(LeadForm.waste_destination)
async def lead_waste_destination(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 300):
        return
    destination = None if message.text == waste.NO_DESTINATION else message.text.strip()
    await state.update_data(waste_destination=destination)
    await show_lead_step(message, state, LeadForm.date)


@router.message(LeadForm.date)
async def lead_date(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 100):
        return
    await state.update_data(work_date=message.text)
    await show_lead_step(message, state, LeadForm.details)


@router.message(LeadForm.details)
async def lead_details(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 1500):
        return
    await state.update_data(details=message.text)
    await show_lead_step(message, state, LeadForm.payment)


@router.message(LeadForm.payment)
async def lead_payment(message: Message, state: FSMContext) -> None:
    if not message.text or message.text not in payment_preferences.METHODS | {payment_preferences.ANY}:
        await message.answer("Выберите форму оплаты кнопкой из списка.")
        return
    await state.update_data(payment_method=message.text)
    await show_lead_step(message, state, LeadForm.phone)


@router.message(LeadForm.phone)
async def lead_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    try:
        phone = provider_registry._validate_phone(nav.phone_input(message))
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        async with aiosqlite.connect(storage.DB_PATH) as db:
            cursor = await db.execute(
                """INSERT INTO leads(customer_id, customer_name, equipment, place, work_date, details, phone, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (message.from_user.id, message.from_user.full_name, data["equipment"], data["place"],
                 data["work_date"], data["details"], phone, created_at),
            )
            lead_id = cursor.lastrowid
            if (payment_method := data.get("payment_method")) is not None:
                await payment_preferences.write_lead(db, lead_id, payment_method)
            await lead_routing.write(db, lead_id, data["equipment"], data["region"], data["area"])
            if data.get("waste_flow"):
                await waste.write(db, lead_id, data)
            lead = await read_lead(db, lead_id)
            await db.commit()
    except aiosqlite.Error:
        logging.exception("Could not save lead and its routing")
        await message.answer("Не удалось сохранить заявку. Анкета открыта. Повторите отправку номера чуть позже.")
        return

    await state.clear()
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
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_menu:{lead_id}"),
    ]])
    for admin_id in settings.admin_ids:
        await notify(bot, admin_id, "Новая заявка на проверку:\n\n" + lead_text(lead, True), reply_markup=markup)
    await message.answer("Спасибо! Проверим заявку и передадим её подходящим исполнителям.", reply_markup=main_menu())


@router.message(OfferForm.price)
async def offer_price(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 100):
        return
    await state.update_data(price=message.text)
    await OFFER_STEPS.show(message, state, OfferForm.comment)


@router.message(OfferForm.comment)
async def offer_comment(message: Message, state: FSMContext) -> None:
    if not await valid_input(message, 1500):
        return
    await state.update_data(comment=message.text)
    await OFFER_STEPS.show(message, state, OfferForm.phone)


@router.message(OfferForm.phone)
async def offer_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    provider_id = message.from_user.id
    profile = await provider_registry.get_profile(provider_id, storage.DB_PATH)
    raw_phone = nav.phone_input(message)
    if raw_phone == nav.PROFILE_PHONE:
        raw_phone = profile["phone"] if profile else None
    try:
        phone = provider_registry._validate_phone(raw_phone)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    data = await state.get_data()
    lead = await get_lead(int(data["lead_id"]))
    if not lead or lead["status"] != "published":
        await state.clear()
        await message.answer("Заявка уже недоступна.", reply_markup=main_menu())
        return
    provider_name = profile["display_name"] if profile else message.from_user.full_name
    handle = message.from_user.username
    username = f"@{handle}" if handle else "без username"
    async with aiosqlite.connect(storage.DB_PATH) as db:
        methods = await payment_preferences.read_provider(db, provider_id)
    offer = (
        f"🚜 Отклик на заявку №{lead['id']}\n\n"
        f"Исполнитель: {provider_name} ({username})\n"
        f"Цена: {data['price']}\n"
        f"Условия: {data['comment']}\n"
        f"Способы оплаты: {', '.join(methods) if methods else 'Не указаны'}\n"
        f"Телефон: {phone}"
    )
    offer_id = await storage.save_offer(
        lead["id"], message.from_user.id, provider_name, username,
        data["price"], data["comment"], phone,
    )
    await state.clear()
    if offer_id is None:
        duplicate = await storage.has_offer(lead["id"], provider_id)
        await message.answer(
            "Вы уже откликнулись на эту заявку" if duplicate else "Заявка закрыта.",
            reply_markup=main_menu(),
        )
        return
    chat_url = f"https://t.me/{handle}" if handle and re.fullmatch(r"[A-Za-z0-9_]+", handle) else f"tg://user?id={provider_id}"
    contact_buttons = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💬 Написать исполнителю", url=chat_url),
        InlineKeyboardButton(text="📞 Позвонить исполнителю", callback_data=f"offer_call:{offer_id}"),
    ]])
    delivered = await notify(
        bot, lead["customer_id"], "По вашей заявке появился отклик:\n\n" + offer,
        reply_markup=contact_buttons,
    )
    await storage.mark_offer(offer_id, "sent" if delivered else "unconfirmed")
    for admin_id in settings.admin_ids - {lead["customer_id"]}:
        await notify(bot, admin_id, offer + ("" if delivered else "\n⚠️ Доставка заказчику не подтверждена. Отклик сохранён."))
    await message.answer(
        "✅ Отклик сохранён и отправлен заказчику." if delivered else
        "Отклик сохранён. Доставка заказчику не подтверждена, требуется проверка диспетчера.",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data.startswith("offer_call:"))
async def call_provider(callback: CallbackQuery, bot: Bot) -> None:
    offer_id = storage.parse_id(callback.data, "offer_call:")
    contact = await storage.offer_contact(offer_id, callback.from_user.id) if offer_id else None
    if not contact:
        await callback.answer("Контакт недоступен", show_alert=True)
        return
    await callback.answer("Откройте номер в карточке контакта")
    try:
        await bot.send_contact(
            callback.from_user.id, provider_registry._validate_phone(contact[1]),
            contact[0][:64] or "Исполнитель",
        )
    except (ValueError, TelegramAPIError):
        await notify(bot, callback.from_user.id, f"Телефон исполнителя: {contact[1]}")


async def published_customer_markup(bot, lead, sent):
    username = getattr(sent.chat, "username", None)
    if username:
        url = f"https://t.me/{username}/{sent.message_id}"
    elif str(sent.chat.id).startswith("-100"):
        try:
            member = await bot.get_chat_member(sent.chat.id, lead["customer_id"])
        except TelegramAPIError:
            return None
        if member.status not in {"creator", "administrator", "member"} and not getattr(member, "is_member", False):
            return None
        url = f"https://t.me/c/{str(sent.chat.id)[4:]}/{sent.message_id}"
    else:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Посмотреть заявку", url=url),
    ]])


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
    customer_markup = await published_customer_markup(bot, lead, sent)
    await notify(bot, lead["customer_id"], f"Ваша заявка №{lead_id} опубликована", reply_markup=customer_markup)
    try:
        await moderation_history.record_approval(lead['customer_id'], lead_id, storage.DB_PATH)
        await notify_test_providers(bot, lead)
    except aiosqlite.Error:
        logging.exception("Post-publication follow-up failed for lead %s", lead_id)
    if callback.message:
        try:
            await callback.message.edit_text("✅ Заявка опубликована.\n\n" + lead_text(lead, True))
        except TelegramAPIError:
            logging.warning("Cannot update moderation message for lead %s", lead_id)


REJECTION_REASONS = {
    "details": "Недостаточно данных",
    "duplicate": "Повторная заявка",
    "scope": "Услуга не поддерживается",
    "none": "Без указания причины",
}


@router.callback_query(F.data.startswith("reject_menu:"))
async def rejection_menu(callback: CallbackQuery) -> None:
    lead_id = await admin_lead_id(callback, "reject_menu:")
    if lead_id is None:
        return
    lead = await get_lead(lead_id)
    if not lead or lead["status"] != "new":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(text=reason, callback_data=f"reject:{lead_id}:{code}")]
               for code, reason in REJECTION_REASONS.items()]
    buttons.append([InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{lead_id}")])
    await callback.answer()
    await callback.message.answer(
        f"Причина отклонения заявки №{lead_id}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("reject:"))
async def reject_lead(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    _, _, remainder = callback.data.partition(":")
    raw_id, _, code = remainder.partition(":")
    lead_id = storage.parse_id(raw_id, "")
    reason = REJECTION_REASONS.get(code or "none")
    if lead_id is None or reason is None:
        await callback.answer("Некорректная заявка или причина", show_alert=True)
        return
    if not await storage.reject_with_reason(lead_id, reason):
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    try:
        await callback.answer("Заявка отклонена")
    except TelegramAPIError:
        logging.warning("Cannot acknowledge rejection callback for lead %s", lead_id)
    lead = await get_lead(lead_id)
    text = f"Ваша заявка №{lead_id} отклонена."
    if code and code != "none":
        text += f" Причина: {reason}."
    await notify(bot, lead["customer_id"], text)
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
