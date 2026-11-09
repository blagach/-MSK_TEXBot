"""Aiogram router for provider self-service."""
from aiogram import F, Router
import aiosqlite
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message

import provider_registry as registry
import payment_preferences as payments
import storage
import navigation as nav

router = Router()
router.message.filter(F.chat.type == "private")

CONFIRM = "✅ Подтвердить выбор"
SAVE = "✅ Сохранить изменения"
CANCEL = "❌ Отменить изменения"
SKIP = "Пропустить"
EDIT_PAYMENTS = "✏️ Изменить способы оплаты"
PAYMENT_LABELS = {
    payments.CASH: "💵 Наличные",
    payments.NO_VAT: "🏦 Безналичный расчёт без НДС",
    payments.VAT: "🧾 Безналичный расчёт с НДС",
}
MACHINE_LABELS = {
    "category": "Категория", "brand": "Марка", "model": "Модель",
    "specifications": "Характеристики", "attachments": "Навесное оборудование",
    "notes": "Описание", "region": "Регион", "area": "Район",
}
EDIT_FIELDS = {"Имя или компания": "name", "Телефон": "phone", "Способы оплаты": "payment"}


class Register(StatesGroup):
    consent = State()
    name = State()
    phone = State()
    payment = State()


class AddMachine(StatesGroup):
    category = State()
    brand = State()
    model = State()
    specifications = State()
    attachments = State()
    notes = State()
    region = State()
    area = State()


class MachineEdit(StatesGroup):
    field = State()
    value = State()
    confirm = State()


MACHINE_EDIT_STATES = {step.state for step in MachineEdit.__all_states__}


class EditProfile(StatesGroup):
    field = State()
    value = State()
    payment = State()
    confirm = State()


class Services(StatesGroup):
    select = State()


PROFILE_STATES = {step.state for group in (EditProfile, Services) for step in group.__all_states__}


REGISTRATION_STEPS = nav.Form({
    Register.consent: (
        "Для регистрации сохраняем имя или название компании, телефон, "
        "Telegram ID и сведения о технике. Телефон не публикуем в канале. "
        "Согласны на обработку этих данных для подбора заявок?",
        ("Да", "Нет"),
    ),
    Register.name: ("Укажите имя или название компании.",),
    Register.phone: ("Введите телефон или отправьте свой контакт.",
                     (KeyboardButton(text=nav.SHARE_PHONE, request_contact=True),)),
    Register.payment: ("Выберите способы оплаты и подтвердите выбор.",),
})

MACHINE_STEPS = nav.Form({
    AddMachine.category: (
        "Выберите категорию техники:",
        *((category,) for category in sorted(registry.CATEGORIES)),
    ),
    AddMachine.brand: ("Укажите марку техники.",),
    AddMachine.model: ("Укажите модель техники.",),
    AddMachine.specifications: ("Укажите характеристики техники.", (SKIP,)),
    AddMachine.attachments: ("Укажите навесное оборудование.", (SKIP,)),
    AddMachine.notes: ("Добавьте свободное описание.", (SKIP,)),
    AddMachine.region: (
        "Выберите зону работы:",
        ("Москва", "Московская область"), (registry.BOTH_REGIONS,),
    ),
    AddMachine.area: (
        "Укажите район (например, СЗАО или Химки) либо выберите «весь регион».",
        ("весь регион",),
    ),
})


async def provider_keyboard(telegram_id):
    profile = await registry.get_profile(telegram_id, storage.DB_PATH)
    return nav.provider_menu(profile and profile["notifications_enabled"])


async def show_selection(message, state, step, options):
    await state.set_state(step)
    selected = (await state.get_data()).get("selection", [])
    labels = {} if step == Services.select else PAYMENT_LABELS
    rows = [(f"{'✅ ' if value in selected else ''}{labels.get(value, value)}",) for value in options]
    label = "Выберите направления услуг." if step == Services.select else "Выберите способы оплаты."
    await message.answer(label + " Нажмите «Подтвердить выбор», когда закончите.",
                         reply_markup=nav.section_menu(*rows, (CONFIRM,), (CANCEL,)))


async def change_selection(message, state, step, options):
    text = (message.text or "").removeprefix("✅ ")
    if step != Services.select:
        text = next((value for value, label in PAYMENT_LABELS.items() if label == text), text)
    if text not in options:
        await message.answer("Выберите вариант из списка.")
        return
    selected = set((await state.get_data()).get("selection", []))
    selected.symmetric_difference_update({text})
    await state.update_data(selection=sorted(selected))
    await show_selection(message, state, step, options)


async def cancel_provider_change(message, state):
    if (await state.get_data()).get("from_payments"):
        await state.set_state(None)
        await message.answer("Изменения отменены.")
        await my_payment_methods(message, state)
        return
    await nav.return_to_provider_menu(state)
    await message.answer("Изменения отменены.", reply_markup=await provider_keyboard(message.from_user.id))


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
        await message.answer("Вы уже зарегистрированы. Ваши данные доступны в «Моём профиле».", reply_markup=await provider_keyboard(message.from_user.id))
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
        await message.answer("Регистрация не выполнена: согласие не получено.", reply_markup=await provider_keyboard(message.from_user.id))
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
        phone_number = registry._validate_phone(nav.phone_input(message))
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(phone=phone_number)
    await show_selection(message, state, Register.payment, sorted(payments.METHODS))


@router.message(Register.payment)
async def provider_payment(message: Message, state: FSMContext):
    if message.text == CANCEL:
        await cancel_provider_change(message, state)
        return
    if message.text != CONFIRM:
        await change_selection(message, state, Register.payment, payments.PAYMENT_OPTIONS)
        return
    methods = set((await state.get_data()).get("selection", []))
    if not methods:
        await message.answer("Выберите хотя бы один способ оплаты.")
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
    await message.answer(text, reply_markup=await provider_keyboard(message.from_user.id))


@router.message(F.text == nav.ADD_MACHINE)
async def begin_machine(message: Message, state: FSMContext):
    await nav.return_to_provider_menu(state)
    status = await profile_status(message.from_user.id)
    if status is None:
        await message.answer("Сначала зарегистрируйтесь как исполнитель.", reply_markup=await provider_keyboard(message.from_user.id))
        return
    if status[0]:
        await message.answer("Профиль заблокирован. Добавление техники недоступно.", reply_markup=await provider_keyboard(message.from_user.id))
        return
    await MACHINE_STEPS.show(message, state, AddMachine.category)


@router.message(AddMachine.category)
async def machine_category(message: Message, state: FSMContext):
    if message.text not in registry.CATEGORIES:
        await message.answer("Выберите категорию точно из списка.")
        return
    await state.update_data(category=message.text)
    await MACHINE_STEPS.show(message, state, AddMachine.brand)


@router.message(AddMachine.brand)
@router.message(AddMachine.model)
@router.message(AddMachine.specifications)
@router.message(AddMachine.attachments)
@router.message(AddMachine.notes)
async def machine_details(message: Message, state: FSMContext):
    current = await state.get_state()
    fields = (AddMachine.brand, AddMachine.model, AddMachine.specifications, AddMachine.attachments, AddMachine.notes)
    index = next(index for index, step in enumerate(fields) if step.state == current)
    field = registry.MACHINE_FIELDS[index]
    value = (message.text or "").strip()
    if value == SKIP and index >= 2:
        value = ""
    elif value == SKIP or not 1 <= len(value) <= 200:
        await message.answer("Введите от 1 до 200 символов или используйте доступную кнопку.")
        return
    await state.update_data(**{field: value})
    await MACHINE_STEPS.show(message, state, (*fields, AddMachine.region)[index + 1])


@router.message(AddMachine.region)
async def machine_region(message: Message, state: FSMContext):
    if message.text not in registry.SERVICE_REGIONS:
        await message.answer("Выберите Москву, Московскую область либо оба региона.")
        return
    await state.update_data(region=message.text)
    if message.text == registry.BOTH_REGIONS:
        await save_machine(message, state, "весь регион")
    else:
        await MACHINE_STEPS.show(message, state, AddMachine.area)


@router.message(AddMachine.area)
async def machine_area(message: Message, state: FSMContext):
    await save_machine(message, state, message.text)


async def save_machine(message, state, area):
    data = await state.get_data()
    fields = {key: data.get(key, "") for key in registry.MACHINE_FIELDS}
    description = " ".join(value for value in fields.values() if value)[:200] or data["category"]
    try:
        machine_id = await registry.add_equipment(
            message.from_user.id, data["category"], description, data["region"], area,
            storage.DB_PATH, fields=fields,
        )
        text = f"Техника №{machine_id} добавлена."
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await nav.return_to_provider_menu(state)
    await message.answer(text, reply_markup=await provider_keyboard(message.from_user.id))


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
    old = await legacy_provider(message.from_user.id)
    if not items and not old:
        await message.answer("Техника пока не добавлена.", reply_markup=detail_menu)
        return
    await message.answer("Ваша техника", reply_markup=detail_menu)
    for item in items:
        await message.answer(machine_card(item), reply_markup=machine_buttons(item))
    if old:
        await message.answer(f"Из прежней анкеты: {old['equipment'][:500]}; {old['location'][:300]}.")


def machine_card(item):
    text = [f"🚜 Техника №{item['id']} · {item['category']}"]
    if item["brand"] is None:
        text.append(f"Описание: {item['description']}")
    else:
        for key in registry.MACHINE_FIELDS:
            text.append(f"{MACHINE_LABELS[key]}: {item[key] or 'Не указано'}")
    text.extend((f"Регион: {item['region']}, {item['area']}",
                 "✅ Активна" if item["active"] else "⏸ Приостановлена"))
    return "\n".join(text)


def machine_buttons(item):
    target = "pause" if item["active"] else "activate"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"machine:edit:{item['id']}"),
        InlineKeyboardButton(text="⏸ Приостановить" if item["active"] else "▶️ Активировать",
                             callback_data=f"machine:{target}:{item['id']}"),
    ]])


@router.callback_query(F.data.startswith("machine:"))
async def machine_action(callback: CallbackQuery, state: FSMContext):
    _, _, action = callback.data.partition(":")
    operation, _, raw_id = action.partition(":")
    machine_id = storage.parse_id(raw_id, "")
    user_id = callback.from_user.id
    items = await registry.list_equipment(user_id, storage.DB_PATH)
    item = next((item for item in items if item["id"] == machine_id), None)
    profile = await registry.get_profile(user_id, storage.DB_PATH)
    if not item or not profile or profile["blocked"] or operation not in {"edit", "pause", "activate"}:
        await callback.answer("Карточка недоступна", show_alert=True)
        return
    if operation == "edit":
        if await state.get_state():
            await callback.answer("Сначала завершите или отмените открытую анкету.", show_alert=True)
            return
        await nav.return_to_provider_menu(state)
        await state.update_data(machine_id=machine_id)
        await state.set_state(MachineEdit.field)
        await callback.answer()
        await callback.message.answer(
            f"Что изменить в карточке техники №{machine_id}?",
            reply_markup=nav.section_menu(*((label,) for label in MACHINE_LABELS.values()), (CANCEL,)),
        )
        return
    active = operation == "activate"
    if not await registry.change_equipment_status(user_id, machine_id, active, storage.DB_PATH):
        await callback.answer("Карточка недоступна", show_alert=True)
        return
    await callback.answer("Статус сохранён")
    if bool(item["active"]) != active:
        item["active"] = active
        await callback.message.edit_text(machine_card(item), reply_markup=machine_buttons(item))


async def return_to_machine_card(message, state, status):
    machine_id = (await state.get_data()).get("machine_id")
    items = await registry.list_equipment(message.from_user.id, storage.DB_PATH)
    item = next((item for item in items if item["id"] == machine_id), None)
    await nav.return_to_provider_menu(state)
    await nav.remember_screen(state, nav.MY_MACHINES, default_parent=nav.PROVIDERS)
    await message.answer(status, reply_markup=nav.section_menu())
    if item:
        await message.answer(machine_card(item), reply_markup=machine_buttons(item))


@router.message(StateFilter(*MACHINE_EDIT_STATES), F.text == CANCEL)
async def cancel_machine_edit(message: Message, state: FSMContext):
    await return_to_machine_card(message, state, "Изменения отменены.")


@router.message(MachineEdit.field)
async def select_machine_field(message: Message, state: FSMContext):
    field = next((field for field, label in MACHINE_LABELS.items() if label == message.text), None)
    if field is None:
        await message.answer("Выберите поле из списка.")
        return
    await state.update_data(machine_field=field)
    await state.set_state(MachineEdit.value)
    choices = {
        "category": sorted(registry.CATEGORIES), "region": sorted(registry.SERVICE_REGIONS),
        "area": ["весь регион"], "specifications": [SKIP], "attachments": [SKIP], "notes": [SKIP],
    }
    await message.answer(f"Новое значение: {MACHINE_LABELS[field]}",
                         reply_markup=nav.section_menu(*((value,) for value in choices.get(field, [])), (CANCEL,)))


@router.message(MachineEdit.value)
async def enter_machine_field(message: Message, state: FSMContext):
    if message.text is None:
        await message.answer("Введите значение текстом.")
        return
    field = (await state.get_data())["machine_field"]
    value = message.text.strip()
    if value == SKIP and field in {"specifications", "attachments", "notes"}:
        value = ""
    try:
        registry.validate_equipment_field(field, value)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(machine_value=value)
    await state.set_state(MachineEdit.confirm)
    await message.answer(f"{MACHINE_LABELS[field]}: {value or 'Не указано'}",
                         reply_markup=nav.section_menu((SAVE,), (CANCEL,)))


@router.message(MachineEdit.confirm)
async def confirm_machine_field(message: Message, state: FSMContext):
    if message.text != SAVE:
        await message.answer("Подтвердите или отмените изменения.")
        return
    data = await state.get_data()
    updated = await registry.edit_equipment_field(
        message.from_user.id, data["machine_id"], data["machine_field"], data["machine_value"], storage.DB_PATH,
    )
    await return_to_machine_card(message, state, "Изменение сохранено." if updated else "Карточка недоступна.")


@router.message(F.text.in_({
    nav.ENABLE_NOTIFICATIONS, nav.DISABLE_NOTIFICATIONS, nav.NOTIFICATIONS_ON, nav.NOTIFICATIONS_OFF,
}))
async def notifications(message: Message, state: FSMContext):
    await nav.return_to_provider_menu(state)
    user_id = message.from_user.id
    profile = await registry.get_profile(user_id, storage.DB_PATH)
    if not profile or profile["blocked"]:
        await message.answer("Сначала зарегистрируйтесь; заблокированный профиль недоступен.",
                             reply_markup=await provider_keyboard(user_id))
        return
    if message.text in {nav.NOTIFICATIONS_ON, nav.NOTIFICATIONS_OFF}:
        await registry.toggle_notifications(user_id, message.text == nav.NOTIFICATIONS_ON, storage.DB_PATH)
    else:
        await registry.set_notifications(user_id, message.text == nav.ENABLE_NOTIFICATIONS, storage.DB_PATH)
    profile = await registry.get_profile(user_id, storage.DB_PATH)
    enabled = profile["notifications_enabled"]
    await message.answer(nav.notification_label(enabled), reply_markup=nav.provider_menu(enabled))


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
        methods = await payments.read_provider(db, message.from_user.id)

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
        f"{nav.notification_label(profile['notifications_enabled'])}\n"
        f"Статус: {'заблокирован' if profile['blocked'] else 'активен'}",
        reply_markup=nav.section_menu((nav.EDIT_PROFILE,)) if not profile["blocked"] else detail_menu,
    )


@router.message(F.text == nav.PAYMENT)
async def my_payment_methods(message: Message, state: FSMContext):
    await nav.remember_screen(state, nav.PAYMENT, default_parent=nav.PROVIDERS)
    profile = await registry.get_profile(message.from_user.id, storage.DB_PATH)
    detail_menu = nav.section_menu((EDIT_PAYMENTS,)) if profile and not profile["blocked"] else nav.section_menu()
    async with aiosqlite.connect(storage.DB_PATH) as db:
        methods = await payments.read_provider(db, message.from_user.id)
    if not methods:
        await message.answer("Способы оплаты пока не указаны.", reply_markup=detail_menu)
        return
    await message.answer(
        "💳 Принимаемые формы оплаты:\n" + "\n".join(f"• {method}" for method in methods),
        reply_markup=detail_menu,
    )

@router.message(F.text == EDIT_PAYMENTS)
async def edit_payment_methods(message: Message, state: FSMContext):
    profile = await registry.get_profile(message.from_user.id, storage.DB_PATH)
    if not profile or profile["blocked"]:
        await message.answer("Редактирование профиля недоступно.")
        return
    await nav.remember_screen(state, nav.PAYMENT, default_parent=nav.PROVIDERS)
    await begin_payment_edit(message, state, from_payments=True)


async def begin_payment_edit(message, state, *, from_payments=False):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        current = await payments.read_provider(db, message.from_user.id)
    await state.update_data(edit_field="payment", selection=current, from_payments=from_payments)
    await show_selection(message, state, EditProfile.payment, payments.PAYMENT_OPTIONS)


@router.message(F.text == nav.EDIT_PROFILE)
async def edit_profile_menu(message: Message, state: FSMContext):
    profile = await registry.get_profile(message.from_user.id, storage.DB_PATH)
    if not profile or profile["blocked"]:
        await message.answer("Редактирование профиля недоступно.")
        return
    await state.set_state(EditProfile.field)
    await message.answer("Что изменить?", reply_markup=nav.section_menu(
        *((label,) for label in EDIT_FIELDS), (CANCEL,),
    ))


@router.message(EditProfile.field)
async def edit_profile_field(message: Message, state: FSMContext):
    if message.text == CANCEL:
        await cancel_provider_change(message, state)
        return
    field = EDIT_FIELDS.get(message.text)
    if field is None:
        await message.answer("Выберите поле из списка.")
        return
    await state.update_data(edit_field=field)
    if field == "payment":
        await begin_payment_edit(message, state)
    else:
        await state.set_state(EditProfile.value)
        keyboard = nav.section_menu((CANCEL,))
        if field == "phone":
            keyboard = nav.section_menu((KeyboardButton(text=nav.SHARE_PHONE, request_contact=True),), (CANCEL,))
        await message.answer("Введите новое имя." if field == "name" else "Введите телефон или отправьте контакт.",
                             reply_markup=keyboard)


@router.message(EditProfile.value)
async def edit_profile_value(message: Message, state: FSMContext):
    if message.text == CANCEL:
        await cancel_provider_change(message, state)
        return
    field = (await state.get_data())["edit_field"]
    try:
        value = registry._validate_name(message.text) if field == "name" else registry._validate_phone(nav.phone_input(message))
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(edit_value=value)
    await state.set_state(EditProfile.confirm)
    await message.answer(f"Новое значение: {value}", reply_markup=nav.section_menu((SAVE,), (CANCEL,)))


@router.message(EditProfile.confirm)
@router.message(EditProfile.payment)
async def edit_profile_confirm(message: Message, state: FSMContext):
    if message.text == CANCEL:
        await cancel_provider_change(message, state)
        return
    data = await state.get_data()
    field = data["edit_field"]
    if field == "payment":
        if message.text != CONFIRM:
            await change_selection(message, state, EditProfile.payment, payments.PAYMENT_OPTIONS)
            return
        value = data.get("selection", [])
        if not value:
            await message.answer("Выберите хотя бы один способ оплаты.")
            return
    else:
        if message.text != SAVE:
            await message.answer("Подтвердите или отмените изменения.")
            return
        value = data["edit_value"]
    updated = await registry.edit_profile(message.from_user.id, field, value, storage.DB_PATH)
    if updated and data.get("from_payments"):
        await state.set_state(None)
        await message.answer("Способы оплаты сохранены.")
        await my_payment_methods(message, state)
    elif updated:
        await nav.return_to_provider_menu(state)
        await my_profile(message, state)
    else:
        await nav.return_to_provider_menu(state)
        await message.answer("Редактирование профиля недоступно.")


@router.message(F.text == nav.SERVICES)
async def services_menu(message: Message, state: FSMContext):
    profile = await registry.get_profile(message.from_user.id, storage.DB_PATH)
    if not profile or profile["blocked"]:
        await message.answer("Сначала зарегистрируйтесь как исполнитель.")
        return
    selected = await registry.provider_services(message.from_user.id, storage.DB_PATH)
    await state.update_data(selection=selected)
    await message.answer("Выбор услуги не подтверждает наличие лицензии, разрешения или права приёма отходов.")
    await show_selection(message, state, Services.select, registry.SERVICE_DIRECTIONS)


@router.message(Services.select)
async def select_services(message: Message, state: FSMContext):
    if message.text == CANCEL:
        await cancel_provider_change(message, state)
        return
    if message.text != CONFIRM:
        await change_selection(message, state, Services.select, registry.SERVICE_DIRECTIONS)
        return
    selected = (await state.get_data()).get("selection", [])
    saved = await registry.save_services(message.from_user.id, selected, storage.DB_PATH)
    await nav.return_to_provider_menu(state)
    await message.answer("Направления сохранены." if saved else "Профиль недоступен.",
                         reply_markup=await provider_keyboard(message.from_user.id))
