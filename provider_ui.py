"""Aiogram router for provider self-service. Attach to Dispatcher after init_schema.

This module does not change existing bot.py or deploy anything.
"""
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

import provider_registry as registry
import payment_preferences as payments
import storage

router = Router()

MENU = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🚜 Зарегистрироваться как исполнитель")],
    [KeyboardButton(text="➕ Добавить технику"), KeyboardButton(text="📋 Моя техника")],
    [KeyboardButton(text="🔔 Уведомления вкл"), KeyboardButton(text="🔕 Уведомления выкл")],
], resize_keyboard=True)


class Register(StatesGroup):
    consent = State()
    name = State()
    phone = State()
    payment = State()


class AddMachine(StatesGroup):
    category = State()
    description = State()
    region = State()
    area = State()


@router.message(F.text == "🚜 Зарегистрироваться как исполнитель")
async def begin_register(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Register.consent)
    await message.answer(
        "Для регистрации сохраняем имя или название компании, телефон, "
        "Telegram ID и сведения о технике. Телефон не публикуем в канале. "
        "Согласны на обработку этих данных для подбора заявок? Ответьте «Да» или «Нет»."
    )


@router.message(Register.consent)
async def consent(message: Message, state: FSMContext):
    if not message.text or message.text.strip().lower() != "да":
        await state.clear()
        await message.answer("Регистрация не выполнена: согласие не получено.")
        return
    await state.update_data(consent=True)
    await state.set_state(Register.name)
    await message.answer("Укажите имя или название компании.")


@router.message(Register.name)
async def name(message: Message, state: FSMContext):
    try:
        name = registry._validate_name(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(name=name)
    await state.set_state(Register.phone)
    await message.answer("Укажите телефон: +7XXXXXXXXXX или 8XXXXXXXXXX.")


@router.message(Register.phone)
async def phone(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        await registry.register_provider(message.from_user.id, data["name"], message.text,
                                         data["consent"], db_path=storage.DB_PATH)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.set_state(Register.payment)
    await message.answer("Какие формы оплаты принимаете? Укажите один или несколько вариантов через запятую:\n" + "\n".join(sorted(payments.METHODS)))


@router.message(Register.payment)
async def provider_payment(message: Message, state: FSMContext):
    methods = {part.strip() for part in (message.text or "").split(",")}
    if not methods or not methods <= payments.METHODS:
        await message.answer("Укажите формы оплаты точно из списка, через запятую.")
        return
    await payments.init_schema(storage.DB_PATH)
    await payments.save_provider(message.from_user.id, methods, storage.DB_PATH)
    await state.clear()
    await message.answer("Профиль и формы оплаты сохранены. Добавьте технику через меню.", reply_markup=MENU)


@router.message(F.text == "➕ Добавить технику")
async def begin_machine(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddMachine.category)
    await message.answer("Выберите категорию техники:\n" + "\n".join(sorted(registry.CATEGORIES)))


@router.message(AddMachine.category)
async def machine_category(message: Message, state: FSMContext):
    if message.text not in registry.CATEGORIES:
        await message.answer("Выберите категорию точно из списка.")
        return
    await state.update_data(category=message.text)
    await state.set_state(AddMachine.description)
    await message.answer("Опишите машину: марка, модель, грузоподъёмность или характеристики.")


@router.message(AddMachine.description)
async def machine_description(message: Message, state: FSMContext):
    if not message.text or not 1 <= len(message.text.strip()) <= 200:
        await message.answer("Описание должно содержать от 1 до 200 символов.")
        return
    await state.update_data(description=message.text.strip())
    await state.set_state(AddMachine.region)
    await message.answer("Выберите зону работы: Москва, Московская область или Москва и Московская область.", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Москва"), KeyboardButton(text="Московская область")], [KeyboardButton(text="Москва и Московская область")]], resize_keyboard=True))


@router.message(AddMachine.region)
async def machine_region(message: Message, state: FSMContext):
    if message.text not in registry.SERVICE_REGIONS:
        await message.answer("Выберите Москву, Московскую область либо оба региона.")
        return
    await state.update_data(region=message.text)
    await state.set_state(AddMachine.area)
    if message.text == registry.BOTH_REGIONS:
        await state.update_data(area="весь регион")
        data = await state.get_data()
        try:
            item_id = await registry.add_equipment(message.from_user.id, data["category"], data["description"], data["region"], "весь регион", db_path=storage.DB_PATH)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.clear()
        await message.answer(f"Техника №{item_id} добавлена: выезд по Москве и всей Московской области.", reply_markup=MENU)
        return
    await message.answer("Укажите район (например, СЗАО или Химки) либо выберите «весь регион».", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="весь регион")]], resize_keyboard=True))


@router.message(AddMachine.area)
async def machine_area(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        item_id = await registry.add_equipment(
            message.from_user.id, data["category"], data["description"],
            data["region"], message.text, db_path=storage.DB_PATH,
        )
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.clear()
    await message.answer(f"Техника №{item_id} добавлена.", reply_markup=MENU)


@router.message(F.text == "📋 Моя техника")
async def my_machines(message: Message):
    items = await registry.list_equipment(message.from_user.id, storage.DB_PATH)
    if not items:
        await message.answer("Техника пока не добавлена.", reply_markup=MENU)
        return
    lines = [f"№{item['id']}: {item['category']} — {item['description']}; "
             f"{item['region']}, {item['area']}; "
             f"{'активна' if item['active'] else 'неактивна'}" for item in items]
    await message.answer("\n".join(lines), reply_markup=MENU)


@router.message(F.text.in_({"🔔 Уведомления вкл", "🔕 Уведомления выкл"}))
async def notifications(message: Message):
    enabled = message.text == "🔔 Уведомления вкл"
    changed = await registry.set_notifications(message.from_user.id, enabled, storage.DB_PATH)
    await message.answer(
        ("Уведомления включены." if enabled else "Уведомления отключены.")
        if changed else "Сначала зарегистрируйтесь; заблокированный профиль недоступен.",
        reply_markup=MENU,
    )
