"""Aiogram router for provider self-service."""
from aiogram import F, Router
import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

import provider_registry as registry
import payment_preferences as payments
import storage
import navigation as nav

router = Router()
router.message.filter(F.chat.type == "private")

MENU = nav.PROVIDER_MENU


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


REGISTRATION_STEPS = nav.Form({
    Register.consent: (
        "Для регистрации сохраняем имя или название компании, телефон, "
        "Telegram ID и сведения о технике. Телефон не публикуем в канале. "
        "Согласны на обработку этих данных для подбора заявок?",
        ("Да", "Нет"),
    ),
    Register.name: ("Укажите имя или название компании.",),
    Register.phone: ("Укажите телефон: +7XXXXXXXXXX или 8XXXXXXXXXX.",),
    Register.payment: (
        "Какие формы оплаты принимаете? Укажите один или несколько вариантов "
        "через запятую либо выберите один кнопкой.",
        *((method,) for method in sorted(payments.METHODS)),
    ),
})

MACHINE_STEPS = nav.Form({
    AddMachine.category: (
        "Выберите категорию техники:",
        *((category,) for category in sorted(registry.CATEGORIES)),
    ),
    AddMachine.description: ("Опишите машину: марка, модель, грузоподъёмность или характеристики.",),
    AddMachine.region: (
        "Выберите зону работы:",
        ("Москва", "Московская область"), (registry.BOTH_REGIONS,),
    ),
    AddMachine.area: (
        "Укажите район (например, СЗАО или Химки) либо выберите «весь регион».",
        ("весь регион",),
    ),
})


async def profile_status(telegram_id):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            "SELECT blocked FROM provider_profiles WHERE telegram_id=?", (telegram_id,)
        ) as cursor:
            return await cursor.fetchone()


@router.message(F.text == nav.REGISTER)
async def begin_register(message: Message, state: FSMContext):
    await nav.return_to_provider_menu(state)
    if await profile_status(message.from_user.id):
        await message.answer("Вы уже зарегистрированы. Ваши данные доступны в «Моём профиле».", reply_markup=MENU)
        return
    await REGISTRATION_STEPS.show(message, state, Register.consent)


@router.message(Register.consent)
async def consent(message: Message, state: FSMContext):
    answer = (message.text or "").strip().lower()
    if answer not in {"да", "нет"}:
        await message.answer("Выберите «Да» или «Нет».")
        return
    if answer == "нет":
        await nav.return_to_provider_menu(state)
        await message.answer("Регистрация не выполнена: согласие не получено.", reply_markup=MENU)
        return
    await REGISTRATION_STEPS.show(message, state, Register.name)


@router.message(Register.name)
async def name(message: Message, state: FSMContext):
    try:
        name = registry._validate_name(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(name=name)
    await REGISTRATION_STEPS.show(message, state, Register.phone)


@router.message(Register.phone)
async def phone(message: Message, state: FSMContext):
    try:
        phone_number = registry._validate_phone(message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(phone=phone_number)
    await REGISTRATION_STEPS.show(message, state, Register.payment)


@router.message(Register.payment)
async def provider_payment(message: Message, state: FSMContext):
    methods = {part.strip() for part in (message.text or "").split(",")}
    if not methods or not methods <= payments.METHODS:
        await message.answer("Укажите формы оплаты точно из списка, через запятую.")
        return
    data = await state.get_data()
    created = await registry.register_new_provider(
        message.from_user.id, data["name"], data["phone"], methods, storage.DB_PATH,
    )
    await nav.return_to_provider_menu(state)
    text = (
        "Профиль и формы оплаты сохранены. Добавьте технику через меню."
        if created else "Вы уже зарегистрированы. Ваши данные доступны в «Моём профиле»."
    )
    await message.answer(text, reply_markup=MENU)


@router.message(F.text == nav.ADD_MACHINE)
async def begin_machine(message: Message, state: FSMContext):
    await nav.return_to_provider_menu(state)
    status = await profile_status(message.from_user.id)
    if status is None:
        await message.answer("Сначала зарегистрируйтесь как исполнитель.", reply_markup=MENU)
        return
    if status[0]:
        await message.answer("Профиль заблокирован. Добавление техники недоступно.", reply_markup=MENU)
        return
    await MACHINE_STEPS.show(message, state, AddMachine.category)


@router.message(AddMachine.category)
async def machine_category(message: Message, state: FSMContext):
    if message.text not in registry.CATEGORIES:
        await message.answer("Выберите категорию точно из списка.")
        return
    await state.update_data(category=message.text)
    await MACHINE_STEPS.show(message, state, AddMachine.description)


@router.message(AddMachine.description)
async def machine_description(message: Message, state: FSMContext):
    if not message.text or not 1 <= len(message.text.strip()) <= 200:
        await message.answer("Описание должно содержать от 1 до 200 символов.")
        return
    await state.update_data(description=message.text.strip())
    await MACHINE_STEPS.show(message, state, AddMachine.region)


@router.message(AddMachine.region)
async def machine_region(message: Message, state: FSMContext):
    if message.text not in registry.SERVICE_REGIONS:
        await message.answer("Выберите Москву, Московскую область либо оба региона.")
        return
    await state.update_data(region=message.text)
    if message.text == registry.BOTH_REGIONS:
        await state.update_data(area="весь регион")
        data = await state.get_data()
        try:
            item_id = await registry.add_equipment(message.from_user.id, data["category"], data["description"], data["region"], "весь регион", db_path=storage.DB_PATH)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await nav.return_to_provider_menu(state)
        await message.answer(f"Техника №{item_id} добавлена: выезд по Москве и всей Московской области.", reply_markup=MENU)
        return
    await MACHINE_STEPS.show(message, state, AddMachine.area)


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
    await nav.return_to_provider_menu(state)
    await message.answer(f"Техника №{item_id} добавлена.", reply_markup=MENU)


async def legacy_provider(telegram_id):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, phone, equipment, location FROM providers WHERE telegram_id=?",
            (telegram_id,),
        ) as cursor:
            return await cursor.fetchone()


@router.message(F.text == nav.MY_MACHINES)
async def my_machines(message: Message, state: FSMContext):
    await nav.remember_screen(state, nav.MY_MACHINES, default_parent=nav.PROVIDERS)
    detail_menu = nav.section_menu()
    items = await registry.list_equipment(message.from_user.id, storage.DB_PATH)
    lines = [
        f"№{item['id']}: {item['category']} — {item['description']}; "
        f"{item['region']}, {item['area']}; "
        f"{'активна' if item['active'] else 'неактивна'}"
        for item in items
    ]
    old = await legacy_provider(message.from_user.id)
    if old:
        lines.append(f"Из прежней анкеты: {old['equipment'][:500]}; {old['location'][:300]}.")
    if not lines:
        await message.answer("Техника пока не добавлена.", reply_markup=detail_menu)
        return
    # Separate chunks fit Telegram's message limit even for a large inventory.
    chunk = []
    length = 0
    for line in lines:
        if chunk and length + len(line) + 1 > 1800:
            await message.answer("\n".join(chunk))
            chunk = []
            length = 0
        chunk.append(line)
        length += len(line) + 1
    await message.answer("\n".join(chunk), reply_markup=detail_menu)


@router.message(F.text.in_({nav.ENABLE_NOTIFICATIONS, nav.DISABLE_NOTIFICATIONS}))
async def notifications(message: Message, state: FSMContext):
    await nav.return_to_provider_menu(state)
    enabled = message.text == nav.ENABLE_NOTIFICATIONS
    changed = await registry.set_notifications(message.from_user.id, enabled, storage.DB_PATH)
    await message.answer(
        ("Уведомления включены." if enabled else "Уведомления отключены.")
        if changed else "Сначала зарегистрируйтесь; заблокированный профиль недоступен.",
        reply_markup=MENU,
    )


@router.message(F.text == nav.PROFILE)
async def my_profile(message: Message, state: FSMContext):
    await nav.remember_screen(state, nav.PROFILE, default_parent=nav.PROVIDERS)
    detail_menu = nav.section_menu()
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT display_name, phone, notifications_enabled, blocked
               FROM provider_profiles WHERE telegram_id=?""",
            (message.from_user.id,),
        ) as cursor:
            profile = await cursor.fetchone()
        async with db.execute(
            "SELECT COUNT(*) FROM provider_equipment WHERE provider_id=?",
            (message.from_user.id,),
        ) as cursor:
            equipment_count = (await cursor.fetchone())[0]
        async with db.execute(
            "SELECT method FROM provider_payment_preferences WHERE provider_id=? ORDER BY method",
            (message.from_user.id,),
        ) as cursor:
            methods = [row[0] for row in await cursor.fetchall()]

    if not profile:
        old = await legacy_provider(message.from_user.id)
        if old:
            await message.answer(
                f"Прежняя анкета исполнителя\nИмя: {(old['name'] or 'Не указано')[:120]}\n"
                f"Телефон: {old['phone']}\n"
                "Сохранённую технику можно посмотреть в «Моей технике». "
                "Чтобы добавить карточки техники, пройдите регистрацию в новом разделе.",
                reply_markup=detail_menu,
            )
        else:
            await message.answer("Профиль исполнителя не найден. Сначала зарегистрируйтесь.", reply_markup=detail_menu)
        return
    payment_text = ", ".join(methods) if methods else "Не указаны"
    await message.answer(
        "👤 Ваш профиль\n"
        f"Имя / компания: {profile['display_name']}\n"
        f"Телефон: {profile['phone']}\n"
        f"Способы оплаты: {payment_text}\n"
        f"Единиц техники: {equipment_count}\n"
        f"Уведомления: {'включены' if profile['notifications_enabled'] else 'выключены'}\n"
        f"Статус: {'заблокирован' if profile['blocked'] else 'активен'}",
        reply_markup=detail_menu,
    )


@router.message(F.text == nav.PAYMENT)
async def my_payment_methods(message: Message, state: FSMContext):
    await nav.remember_screen(state, nav.PAYMENT, default_parent=nav.PROVIDERS)
    detail_menu = nav.section_menu()
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            "SELECT method FROM provider_payment_preferences WHERE provider_id=? ORDER BY method",
            (message.from_user.id,),
        ) as cursor:
            methods = [row[0] for row in await cursor.fetchall()]
    if not methods:
        await message.answer("Способы оплаты пока не указаны.", reply_markup=detail_menu)
        return
    await message.answer(
        "💳 Принимаемые формы оплаты:\n" + "\n".join(f"• {method}" for method in methods),
        reply_markup=detail_menu,
    )
