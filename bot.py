import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: set[int]
    leads_channel_id: int
    payment_url: str
    bot_username: str


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
    )


settings = read_settings()
router = Router()


class LeadForm(StatesGroup):
    equipment = State()
    place = State()
    date = State()
    details = State()
    phone = State()


class ProviderForm(StatesGroup):
    equipment = State()
    location = State()
    phone = State()


class OfferForm(StatesGroup):
    price = State()
    comment = State()
    phone = State()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Оставить заявку")],
            [KeyboardButton(text="🚜 Я исполнитель")],
        ], resize_keyboard=True,
    )


async def init_db() -> None:
    async with aiosqlite.connect("bot.db") as db:
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


def lead_text(lead: aiosqlite.Row, with_phone: bool = False) -> str:
    text = (
        f"📌 Заявка №{lead['id']}\n"
        f"Техника: {lead['equipment']}\n"
        f"Место: {lead['place']}\n"
        f"Когда: {lead['work_date']}\n"
        f"Детали: {lead['details']}"
    )
    if with_phone:
        text += f"\nКонтакт заказчика: {lead['phone']}"
    return text


async def get_lead(lead_id: int):
    async with aiosqlite.connect("bot.db") as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)) as cursor:
            return await cursor.fetchone()


@router.message(CommandStart(deep_link=True))
async def start_offer(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    if not command.args or not command.args.startswith("lead_"):
        await message.answer("Ссылка на заявку устарела. Откройте актуальную заявку в канале.", reply_markup=main_menu())
        return
    lead_id = int(command.args.removeprefix("lead_"))
    lead = await get_lead(lead_id)
    if not lead or lead["status"] != "published":
        await message.answer("Эта заявка уже недоступна.", reply_markup=main_menu())
        return
    await state.update_data(lead_id=lead_id)
    await state.set_state(OfferForm.price)
    await message.answer("Укажите вашу цену по заявке. Например: 28 000 ₽ за смену.")


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Здравствуйте! Здесь можно быстро оставить заявку на аренду строительной или дорожной техники в Москве и МО.",
        reply_markup=main_menu(),
    )


@router.message(F.text == "📝 Оставить заявку")
async def lead_start(message: Message, state: FSMContext) -> None:
    await state.set_state(LeadForm.equipment)
    await message.answer("Какая техника нужна? Например: манипулятор 5 т, мини-экскаватор, каток.")


@router.message(LeadForm.equipment)
async def lead_equipment(message: Message, state: FSMContext) -> None:
    await state.update_data(equipment=message.text)
    await state.set_state(LeadForm.place)
    await message.answer("Где нужна техника? Укажите район, адрес или ближайшее метро.")


@router.message(LeadForm.place)
async def lead_place(message: Message, state: FSMContext) -> None:
    await state.update_data(place=message.text)
    await state.set_state(LeadForm.date)
    await message.answer("На какую дату и время нужна техника?")


@router.message(LeadForm.date)
async def lead_date(message: Message, state: FSMContext) -> None:
    await state.update_data(work_date=message.text)
    await state.set_state(LeadForm.details)
    await message.answer("Опишите работу: объём, срок, груз/навесное оборудование, важные условия.")


@router.message(LeadForm.details)
async def lead_details(message: Message, state: FSMContext) -> None:
    await state.update_data(details=message.text)
    await state.set_state(LeadForm.phone)
    await message.answer("Оставьте номер телефона для связи.")


@router.message(LeadForm.phone)
async def lead_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    async with aiosqlite.connect("bot.db") as db:
        cursor = await db.execute(
            """INSERT INTO leads(customer_id, customer_name, equipment, place, work_date, details, phone, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (message.from_user.id, message.from_user.full_name, data["equipment"], data["place"], data["work_date"], data["details"], message.text, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        lead_id = cursor.lastrowid
    lead = await get_lead(lead_id)
    markup = InlineKeyboardMarkup(inline_keyboard=[[ 
        InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{lead_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{lead_id}"),
    ]])
    for admin_id in settings.admin_ids:
        await bot.send_message(admin_id, "Новая заявка на проверку:\n\n" + lead_text(lead, True), reply_markup=markup)
    await state.clear()
    await message.answer("Спасибо! Проверим заявку и передадим её подходящим исполнителям.", reply_markup=main_menu())


@router.message(F.text == "🚜 Я исполнитель")
async def provider_start(message: Message, state: FSMContext) -> None:
    await state.set_state(ProviderForm.equipment)
    await message.answer("Какую технику вы предоставляете?")


@router.message(ProviderForm.equipment)
async def provider_equipment(message: Message, state: FSMContext) -> None:
    await state.update_data(equipment=message.text)
    await state.set_state(ProviderForm.location)
    await message.answer("В каких районах работаете?")


@router.message(ProviderForm.location)
async def provider_location(message: Message, state: FSMContext) -> None:
    await state.update_data(location=message.text)
    await state.set_state(ProviderForm.phone)
    await message.answer("Оставьте телефон для связи.")


@router.message(ProviderForm.phone)
async def provider_phone(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with aiosqlite.connect("bot.db") as db:
        await db.execute(
            "INSERT OR REPLACE INTO providers(telegram_id, name, equipment, location, phone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (message.from_user.id, message.from_user.full_name, data["equipment"], data["location"], message.text, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    await state.clear()
    await message.answer("Готово. Вы зарегистрированы как исполнитель. Когда появится подходящая заявка, она будет опубликована в канале.", reply_markup=main_menu())


@router.message(OfferForm.price)
async def offer_price(message: Message, state: FSMContext) -> None:
    await state.update_data(price=message.text)
    await state.set_state(OfferForm.comment)
    await message.answer("Коротко опишите условия: какая техника, подача, что включено. Если без комментария — напишите «без комментария».")


@router.message(OfferForm.comment)
async def offer_comment(message: Message, state: FSMContext) -> None:
    await state.update_data(comment=message.text)
    await state.set_state(OfferForm.phone)
    await message.answer("Оставьте телефон для связи с заказчиком.")


@router.message(OfferForm.phone)
async def offer_phone(message: Message, state: FSMContext, bot: Bot) -> None:
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
        f"Телефон: {message.text}"
    )
    await bot.send_message(lead["customer_id"], "По вашей заявке появился отклик:\n\n" + offer)
    for admin_id in settings.admin_ids:
        await bot.send_message(admin_id, offer)
    await state.clear()
    await message.answer("✅ Отклик отправлен заказчику. Если он заинтересуется, свяжется с вами напрямую.", reply_markup=main_menu())


@router.callback_query(F.data.startswith("publish:"))
async def publish_lead(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    lead_id = int(callback.data.split(":")[1])
    lead = await get_lead(lead_id)
    if not lead or lead["status"] != "new":
        await callback.answer("Заявка уже обработана", show_alert=True)
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🚜 Откликнуться",
        url=f"https://t.me/{settings.bot_username}?start=lead_{lead_id}",
    )]])
    await bot.send_message(settings.leads_channel_id, lead_text(lead), reply_markup=markup)
    async with aiosqlite.connect("bot.db") as db:
        await db.execute("UPDATE leads SET status = 'published' WHERE id = ?", (lead_id,))
        await db.commit()
    await callback.message.edit_text("✅ Заявка опубликована в канале.\n\n" + lead_text(lead, True))
    await callback.answer()


@router.callback_query(F.data.startswith("reject:"))
async def reject_lead(callback: CallbackQuery, bot: Bot) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    lead_id = int(callback.data.split(":")[1])
    lead = await get_lead(lead_id)
    if lead:
        async with aiosqlite.connect("bot.db") as db:
            await db.execute("UPDATE leads SET status = 'rejected' WHERE id = ?", (lead_id,))
            await db.commit()
        await bot.send_message(lead["customer_id"], "К сожалению, сейчас не сможем подобрать исполнителя по этой заявке.")
    await callback.message.edit_text("❌ Заявка отклонена.")
    await callback.answer()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=settings.token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
