"""Aiogram router for provider self-service. Attach after init_schema."""
from aiogram import F, Router
import aiosqlite
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, CallbackQuery, Message, ReplyKeyboardMarkup

from draft_storage import offer_draft_ids

import provider_registry as registry
import payment_preferences as payments
import storage
import equipment_catalog

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

AVAILABLE_LEADS = "🔎 Подходящие заявки"
MY_OFFERS = "📋 Мои отклики"
OFFER_DRAFT = "▶️ Продолжить отклик"
HOME = "🏠 Главное меню"
REGISTER_PROVIDER = "🚜 Зарегистрироваться как исполнитель"
LEGACY_PROVIDER = "🛠 Обновить быструю регистрацию"
MY_TECH = "🚜 Моя техника"
MY_REGIONS = "📍 Мои регионы"
INVENTORY = "🚜 Карточки техники"
PAYMENT_METHOD = "💳 Способ оплаты"
PAYMENT_OPTIONS = (payments.CASH, payments.VAT, payments.NO_VAT)
NOTIFICATIONS_ON = "🔔 Уведомления включены"
NOTIFICATIONS_OFF = "🔕 Уведомления выключены"
NOTIFICATION_ACTIONS = {
    NOTIFICATIONS_ON: False,
    NOTIFICATIONS_OFF: True,
    "🔔 Уведомления вкл": True,
    "🔕 Уведомления выкл": False,
}


def section_keyboard(*rows: tuple[str, ...]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True, is_persistent=True,
    )


async def executor_keyboard(user_id: int, state: FSMContext) -> ReplyKeyboardMarkup:
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            """SELECT (SELECT notifications_enabled FROM provider_profiles WHERE telegram_id=?),
                      EXISTS(SELECT 1 FROM providers WHERE telegram_id=?)""", (user_id, user_id),
        ) as cursor:
            notifications_enabled, legacy = await cursor.fetchone()

    draft = (OFFER_DRAFT,) if await offer_draft_ids(state) else ()
    if notifications_enabled is not None:
        notifications = NOTIFICATIONS_ON if notifications_enabled else NOTIFICATIONS_OFF
        rows = [(MY_TECH, MY_REGIONS), (notifications, *draft)]

    else:
        rows = [(MY_OFFERS,)] if legacy else []
        rows.append((REGISTER_PROVIDER,))
        if legacy:
            rows.append((LEGACY_PROVIDER,))
        if draft:
            rows.append(draft)
    return section_keyboard(*rows, (HOME,))


class Register(StatesGroup):
    consent = State()
    name = State()
    phone = State()
    payment = State()


class AlertPreferences(StatesGroup):
    select = State()


class AddMachine(StatesGroup):
    category = State()
    equipment_type = State()
    review = State()
    description = State()
    region = State()
    area = State()


def provider_tools_keyboard():
    return section_keyboard(
        (INVENTORY, "👤 Мой профиль"), ("➕ Добавить технику", PAYMENT_METHOD),
        (MY_OFFERS,), (HOME,),
    )


@router.message(F.text.in_({MY_TECH, MY_REGIONS}))
async def begin_alert_preferences(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        await message.answer("Сначала завершите открытое действие или сохраните его через /menu.")
        return
    async with aiosqlite.connect(storage.DB_PATH) as db:
        profile = await (await db.execute(
            "SELECT blocked FROM provider_profiles WHERE telegram_id=?", (message.from_user.id,),
        )).fetchone()
    if not profile or profile[0]:
        await message.answer("Сначала зарегистрируйтесь; заблокированный профиль недоступен.",
                             reply_markup=await executor_keyboard(message.from_user.id, state))
        return
    current = await registry.alert_preferences(message.from_user.id, storage.DB_PATH)
    await state.set_data({
        "alert_categories": current["categories"], "alert_regions": current["regions"],
        "alert_revision": current["revision"],
    })
    await state.set_state(AlertPreferences.select)
    kind = "regions" if message.text == MY_REGIONS else "categories"
    await message.answer("Настройки получения заявок. Сохранённые карточки техники доступны ниже.",
                         reply_markup=section_keyboard((HOME,)))
    await show_alert_preferences(message, state, kind)


async def show_alert_preferences(message, state, kind, group=None):
    await retire_inline_panel(message, state, "alert_panel")
    data = await state.update_data(alert_kind=kind, alert_group=group)
    if kind == "regions":
        labels = ("Москва", "Московская область")
        selected = data.get("alert_regions", [])
        rows = [[InlineKeyboardButton(text=f"{'✅' if value in selected else '⬜'} {value}",
                                      callback_data=f"provider_alert:region:{index}")]
                for index, value in enumerate(labels)]
        rows.append([InlineKeyboardButton(text="Оба региона", callback_data="provider_alert:both")])
        prompt = "📍 В каких регионах получать заявки?\nВыберите Москву, Московскую область или оба региона."
    elif group is None:
        rows = equipment_catalog.button_rows(
            tuple(name for name, _ in equipment_catalog.GROUPS),
            lambda index, label: InlineKeyboardButton(text=label, callback_data=f"provider_alert:group:{index}"),
        )
        prompt = "🚜 На какую технику получать заявки?\nВыберите один или несколько видов техники."
    else:
        labels = equipment_catalog.GROUPS[group][1]
        selected = data.get("alert_categories", [])
        rows = equipment_catalog.button_rows(
            labels, lambda index, label: InlineKeyboardButton(
                text=f"{'✅' if labels[index] in selected else '⬜'} {label}",
                callback_data=f"provider_alert:type:{index}",
            ),
        )
        rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="provider_alert:back")])
        prompt = "🚜 На какую технику получать заявки?\nВыберите один или несколько видов техники."
    rows.append([InlineKeyboardButton(text="⚙️ Карточки и профиль", callback_data="provider_alert:tools")])
    rows.append([InlineKeyboardButton(text="Сохранить", callback_data="provider_alert:save"),
                 InlineKeyboardButton(text="Отмена", callback_data="provider_alert:cancel")])
    prompt += ("\n\nВыбрано видов: " + str(len(data.get("alert_categories", [])))
               + ". Регионов: " + str(len(data.get("alert_regions", [])))
               + ". Новые настройки применяются после сохранения."
               + "\nРегионы: " + (", ".join(data.get("alert_regions", [])) or "не выбраны")
               + "\nПосле сохранения заявки подбираются по всему выбранному региону.")
    sent = await message.answer(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.update_data(alert_panel=sent.message_id)


@router.callback_query(F.data.startswith("provider_alert:"))
async def alert_preference_action(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if (await state.get_state() != AlertPreferences.select.state or not isinstance(callback.message, Message)
            or callback.message.message_id != data.get("alert_panel")
            or callback.message.chat.id != callback.from_user.id):
        await callback.answer("Этот экран устарел. Откройте настройки снова.")
        return
    action = callback.data.removeprefix("provider_alert:")
    kind = data["alert_kind"]
    group = data.get("alert_group")
    await callback.answer()
    if action == "tools":
        import bot as app
        await app.pause_form(callback.message, state)
        await callback.message.answer("Карточки техники и профиль. Выбор уведомлений сохранён в черновике.",
                                      reply_markup=provider_tools_keyboard())
        return
    if action == "cancel":
        await retire_inline_panel(callback.message, state, "alert_panel")
        await state.clear()
        await callback.message.answer("Настройки оставлены без изменений.",
                                      reply_markup=await executor_keyboard(callback.from_user.id, state))
        return
    if action == "save":
        if not data["alert_categories"]:
            await callback.message.answer("Выберите хотя бы один вид техники.")
            await show_alert_preferences(callback.message, state, "categories")
            return
        if not data["alert_regions"]:
            await callback.message.answer("Выберите хотя бы один регион.")
            await show_alert_preferences(callback.message, state, "regions")
            return
        try:
            saved = await registry.save_alert_preferences(
                callback.from_user.id, data["alert_categories"], data["alert_regions"],
                data["alert_revision"], storage.DB_PATH,
            )
        except ValueError as error:
            await callback.message.answer(str(error))
            return
        if not saved:
            await callback.message.answer("Настройки уже изменились в другом сеансе. Ваш выбор остался в черновике. "
                                          "Отмените изменение и откройте настройки снова для сверки.")
            return
        await retire_inline_panel(callback.message, state, "alert_panel")
        await state.clear()
        await callback.message.answer(
            "Настройки сохранены. Подходящие заявки будут приходить отдельными сообщениями. "
            "Уведомления зависят от сохранённого переключателя. Приостановленные карточки учитываются.",
            reply_markup=await executor_keyboard(callback.from_user.id, state),
        )
        return
    if kind == "categories" and action.startswith("group:") and action[6:].isdecimal():
        index = int(action[6:])
        if index < len(equipment_catalog.GROUPS):
            await show_alert_preferences(callback.message, state, kind, index)
        return
    if kind == "categories" and group is not None and action.startswith("type:") and action[5:].isdecimal():
        index = int(action[5:])
        labels = equipment_catalog.GROUPS[group][1]
        if index >= len(labels):
            return
        selected = set(data["alert_categories"]) ^ {labels[index]}
        ordered = [value for _, values in equipment_catalog.GROUPS for value in values if value in selected]
        await state.update_data(alert_categories=ordered)
    elif kind == "categories" and action == "back":
        group = None
    elif kind == "regions" and action in {"region:0", "region:1", "both"}:
        selected = set(data["alert_regions"])
        if action == "both":
            selected = set(registry.REGIONS)
        else:
            value = ("Москва", "Московская область")[int(action[-1])]
            selected ^= {value}
        await state.update_data(alert_regions=sorted(selected))
    else:
        return
    await show_alert_preferences(callback.message, state, kind, group)


@router.message(F.text == REGISTER_PROVIDER)
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
    await start_payment_selection(message, state, message.from_user.id)


async def start_payment_selection(message, state, user_id):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute("SELECT method FROM provider_payment_preferences WHERE provider_id=?", (user_id,)) as cursor:
            selected = {row[0] for row in await cursor.fetchall()}
    await state.set_state(Register.payment)
    await show_payment_selection(message, state, selected)


async def show_payment_selection(message, state, selected, *, update=False):
    rows = [
        [InlineKeyboardButton(text=f"{'✅' if method in selected else '⬜'} {method}",
                              callback_data=f"provider_payment:pick:{index}")]
        for index, method in enumerate(PAYMENT_OPTIONS)
    ]
    rows.append([InlineKeyboardButton(text="Сохранить", callback_data="provider_payment:save"),
                 InlineKeyboardButton(text="Отмена", callback_data="provider_payment:cancel")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if update:
        try:
            await message.edit_reply_markup(reply_markup=markup)
        except TelegramAPIError:
            await retire_payment_panel(message, state)
        else:
            await state.update_data(payment_selection=sorted(selected))
            return
    sent = await message.answer(
        "Выберите один или несколько способов оплаты и нажмите «Сохранить». "
        "Также можно перечислить варианты через запятую.",
        reply_markup=markup,
    )
    await state.update_data(payment_selection=sorted(selected), payment_panel=sent.message_id)


async def retire_payment_panel(message, state):
    await retire_inline_panel(message, state, "payment_panel")


async def retire_inline_panel(message, state, key):
    panel = (await state.get_data()).get(key)
    if panel is None:
        return
    await state.update_data(**{key: None})
    try:
        await message.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=panel, reply_markup=None)
    except TelegramAPIError:
        pass  # Old callbacks remain inactive because the stored panel ID was cleared.


@router.callback_query(F.data.startswith("provider_payment:pick:") | F.data.in_({
    "provider_payment:save", "provider_payment:cancel",
}))
async def payment_selection_callback(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if (await state.get_state() != Register.payment.state or not isinstance(callback.message, Message)
            or callback.message.message_id != data.get("payment_panel")):
        await callback.answer("Этот экран устарел. Откройте способы оплаты снова.")
        return
    action = callback.data.rsplit(":", 1)[-1]
    selected = set(data["payment_selection"])
    if action == "save" and not selected:
        await callback.answer("Выберите хотя бы один способ оплаты.", show_alert=True)
        return
    await callback.answer()
    if action == "save":
        await finish_payment_selection(callback.message, state, callback.from_user.id, selected)
    elif action == "cancel":
        await retire_payment_panel(callback.message, state)
        await state.clear()
        await callback.message.answer("Способы оплаты оставлены без изменений.",
                                      reply_markup=await executor_keyboard(callback.from_user.id, state))
    elif action in {"0", "1", "2"}:
        await show_payment_selection(callback.message, state, selected ^ {PAYMENT_OPTIONS[int(action)]}, update=True)


@router.message(Register.payment)
async def provider_payment(message: Message, state: FSMContext):
    methods = {part.strip() for part in (message.text or "").split(",")}
    if not methods or not methods <= payments.METHODS:
        await message.answer("Укажите формы оплаты точно из списка, через запятую.")
        return
    await finish_payment_selection(message, state, message.from_user.id, methods)


async def finish_payment_selection(message, state, user_id, methods):
    await payments.save_provider(user_id, methods, storage.DB_PATH)
    await retire_payment_panel(message, state)
    await state.clear()
    await message.answer("Способы оплаты сохранены.", reply_markup=await executor_keyboard(user_id, state))


async def machine_catalog(message, state, group=None):
    await retire_inline_panel(message, state, "machine_panel")
    if group is None:
        await state.set_state(AddMachine.category)
        labels = tuple(name for name, _ in equipment_catalog.GROUPS)
    else:
        await state.set_state(AddMachine.equipment_type)
        labels = equipment_catalog.GROUPS[group][1]
    rows = equipment_catalog.button_rows(
        labels, lambda index, label: InlineKeyboardButton(text=label, callback_data=f"machine_catalog:{index}"),
    )
    if group is not None:
        rows.append([InlineKeyboardButton(text="⬅️ К категориям", callback_data="machine_catalog:back")])
    sent = await message.answer("Выберите категорию техники:" if group is None else "Выберите вид техники:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.update_data(machine_group=group, machine_panel=sent.message_id)


@router.message(F.text == "➕ Добавить технику")
async def begin_machine(message: Message, state: FSMContext):
    await state.clear()
    await machine_catalog(message, state)


@router.callback_query(F.data.startswith("machine_catalog:"))
async def machine_catalog_action(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    step = await state.get_state()
    if not isinstance(callback.message, Message) or callback.message.message_id != data.get("machine_panel") or step not in (
        AddMachine.category.state, AddMachine.equipment_type.state,
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    action = callback.data.partition(":")[2]
    if action == "back" and step == AddMachine.equipment_type.state:
        await callback.answer()
        await machine_catalog(callback.message, state)
        return
    if not action.isascii() or not action.isdecimal() or len(action) > 2:
        await callback.answer("Неизвестный вид.")
        return
    index = int(action)
    await callback.answer()
    if step == AddMachine.category.state:
        if index < len(equipment_catalog.GROUPS):
            await machine_catalog(callback.message, state, index)
        return
    kinds = equipment_catalog.GROUPS[data["machine_group"]][1]
    if index < len(kinds):
        await select_machine(callback.message, state, kinds[index])


async def select_machine(message, state, category):
    await retire_inline_panel(message, state, "machine_panel")
    await state.update_data(category=category)
    if (await state.get_data()).get("edit_field") == "category":
        await review_machine(message, state, (await state.get_data())["area"])
        return
    await state.set_state(AddMachine.description)
    prompt = ("Укажите название и характеристики техники." if category == equipment_catalog.OTHER
              else "Опишите машину: марка, модель, грузоподъёмность или характеристики.")
    await message.answer(prompt)


@router.message(AddMachine.category, F.text)
async def machine_category(message: Message, state: FSMContext):
    if message.text not in registry.CATEGORIES:
        await message.answer("Выберите категорию кнопкой или откройте /menu.")
        return
    await select_machine(message, state, message.text)


@router.message(AddMachine.description)
async def machine_description(message: Message, state: FSMContext):
    if not message.text or not 1 <= len(message.text.strip()) <= 200:
        await message.answer("Описание должно содержать от 1 до 200 символов.")
        return
    data = await state.update_data(description=message.text.strip())
    if data.get("edit_field") == "description":
        await review_machine(message, state, data["area"])
        return
    await ask_machine_region(message, state)


async def ask_machine_region(message, state):
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
        await review_machine(message, state, "весь регион")
        return
    await message.answer("Укажите район (например, СЗАО или Химки) либо выберите «весь регион».", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="весь регион")]], resize_keyboard=True))


@router.message(AddMachine.area)
async def machine_area(message: Message, state: FSMContext):
    area = (message.text or "").strip()
    if not 1 <= len(area) <= 120:
        await message.answer("Укажите район или «весь регион».")
        return
    await review_machine(message, state, area)


async def review_machine(message, state, area):
    await retire_inline_panel(message, state, "machine_panel")
    data = await state.update_data(area=area)
    await state.set_state(AddMachine.review)
    sent = await message.answer(
        f"Проверьте карточку техники:\n{data['category']} · {data['description']}\n{data['region']}, {area}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Сохранить", callback_data="machine_review:save"),
            InlineKeyboardButton(text="✏️ Изменить данные", callback_data="machine_review:edit"),
        ]]),
    )
    await state.update_data(machine_panel=sent.message_id, machine_choosing_field=False)


@router.callback_query(F.data.startswith("machine_review:"))
async def machine_review(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if await state.get_state() != AddMachine.review.state or not isinstance(callback.message, Message) or (
        callback.message.message_id != data.get("machine_panel")
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    await callback.answer()
    action = callback.data.partition(":")[2]
    if action == "edit":
        await machine_edit_fields(callback.message, state)
    elif action == "save":
        try:
            if data.get("edit_machine"):
                if not await registry.update_equipment(
                    callback.from_user.id, data["edit_machine"], category=data["category"],
                    description=data["description"], region=data["region"], area=data["area"],
                    active=data["machine_active"], db_path=storage.DB_PATH,
                ):
                    await callback.message.answer("Карточка недоступна.")
                    return
                item_id = data["edit_machine"]
            else:
                item_id = await registry.add_equipment(
                    callback.from_user.id, data["category"], data["description"], data["region"],
                    data["area"], db_path=storage.DB_PATH,
                )
        except ValueError as exc:
            await callback.message.answer(str(exc))
            return
        await retire_inline_panel(callback.message, state, "machine_panel")
        await state.clear()
        await callback.message.answer(f"Техника №{item_id} сохранена.",
                                      reply_markup=await executor_keyboard(callback.from_user.id, state))


@router.message(F.text.in_({INVENTORY, "📋 Моя техника"}))
async def my_machines(message: Message, state: FSMContext):
    items = await registry.list_equipment(message.from_user.id, storage.DB_PATH)
    for item in items:
        await show_machine(message, item)
    await message.answer("Ваша техника" if items else "Техника пока не добавлена.",
                         reply_markup=await executor_keyboard(message.from_user.id, state))


async def show_machine(message, item):
    active = item["active"]
    await message.answer(
        f"Техника №{item['id']} · {item['category']}\n{item['description']}\n"
        f"{item['region']}, {item['area']}\n{'✅ Активна' if active else '⏸ Приостановлена'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"machine:edit:{item['id']}"),
            InlineKeyboardButton(text="⏸ Приостановить" if active else "▶️ Активировать",
                                 callback_data=f"machine:{'pause' if active else 'activate'}:{item['id']}"),
        ]]),
    )


@router.callback_query(F.data.startswith("machine:"))
async def machine_action(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3 or not isinstance(callback.message, Message):
        await callback.answer("Карточка недоступна.")
        return
    item = next((item for item in await registry.list_equipment(callback.from_user.id, storage.DB_PATH)
                 if str(item["id"]) == parts[2]), None)
    if not item or await state.get_state():
        await callback.answer("Завершите текущее действие или откройте главное меню.", show_alert=True)
        return
    action = parts[1]
    await callback.answer()
    if action == "edit":
        await state.update_data(edit_machine=item["id"], machine_active=item["active"],
                                **{field: item[field] for field in ("category", "description", "region", "area")})
        await machine_edit_fields(callback.message, state)
    elif action in {"pause", "activate"}:
        active = action == "activate"
        if active == item["active"]:
            return
        changed = await registry.update_equipment(
            callback.from_user.id, item["id"], category=item["category"], description=item["description"],
            region=item["region"], area=item["area"], active=active, db_path=storage.DB_PATH,
        )
        if changed:
            await callback.message.edit_reply_markup(reply_markup=None)
            await show_machine(callback.message, {**item, "active": active})


async def machine_edit_fields(message, state):
    await retire_inline_panel(message, state, "machine_panel")
    await state.set_state(AddMachine.review)
    sent = await message.answer("Выберите поле техники:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Вид техники", callback_data="machine_field:category"),
         InlineKeyboardButton(text="Описание", callback_data="machine_field:description")],
        [InlineKeyboardButton(text="Зона работы", callback_data="machine_field:region"),
         InlineKeyboardButton(text="Отмена", callback_data="machine_field:cancel")],
    ]))
    await state.update_data(machine_panel=sent.message_id, machine_choosing_field=True)


@router.callback_query(F.data.startswith("machine_field:"))
async def machine_edit_field(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not isinstance(callback.message, Message) or callback.message.message_id != data.get("machine_panel") or (
        await state.get_state() != AddMachine.review.state
    ):
        await callback.answer("Этот шаг уже неактуален.")
        return
    field = callback.data.partition(":")[2]
    if field not in {"category", "description", "region", "cancel"}:
        await callback.answer("Неизвестное поле.")
        return
    await callback.answer()
    await retire_inline_panel(callback.message, state, "machine_panel")
    if field == "cancel":
        await state.clear()
        item = next((item for item in await registry.list_equipment(callback.from_user.id, storage.DB_PATH)
                     if item["id"] == data.get("edit_machine")), None)
        if item:
            await show_machine(callback.message, item)
        else:
            await callback.message.answer("Изменения не сохранены.", reply_markup=await executor_keyboard(
                callback.from_user.id, state,
            ))
        return
    await state.update_data(edit_field=field)
    if field == "category":
        await machine_catalog(callback.message, state)
    elif field == "description":
        await state.set_state(AddMachine.description)
        await callback.message.answer("Введите новое описание техники.")
    else:
        await ask_machine_region(callback.message, state)


@router.message(F.text.in_(NOTIFICATION_ACTIONS))
async def notifications(message: Message, state: FSMContext):
    enabled = NOTIFICATION_ACTIONS[message.text]
    changed = await registry.set_notifications(message.from_user.id, enabled, storage.DB_PATH)
    await message.answer(
        ("Уведомления включены." if enabled else "Уведомления отключены.")
        if changed else "Сначала зарегистрируйтесь; заблокированный профиль недоступен.",
        reply_markup=await executor_keyboard(message.from_user.id, state),
    )


@router.message(F.text == "👤 Мой профиль")
async def my_profile(message: Message, state: FSMContext):
    async with aiosqlite.connect(storage.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT display_name, phone, notifications_enabled, blocked,
                      EXISTS(SELECT 1 FROM providers WHERE providers.telegram_id=provider_profiles.telegram_id) AS legacy
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
        await message.answer("Профиль исполнителя не найден. Сначала зарегистрируйтесь.", reply_markup=await executor_keyboard(message.from_user.id, state))
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
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=LEGACY_PROVIDER, callback_data="provider_legacy:edit"),
        ]]) if profile["legacy"] else await executor_keyboard(message.from_user.id, state),
    )


@router.message(F.text.in_({PAYMENT_METHOD, "💳 Способы оплаты"}))
async def my_payment_methods(message: Message, state: FSMContext):
    await payments.init_schema(storage.DB_PATH)
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute(
            "SELECT method FROM provider_payment_preferences WHERE provider_id=? ORDER BY method",
            (message.from_user.id,),
        ) as cursor:
            methods = [row[0] for row in await cursor.fetchall()]
    await message.answer(
        "💳 Принимаемые формы оплаты:\n" + ("\n".join(f"• {method}" for method in methods) or "Не указаны"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Изменить способы оплаты", callback_data="provider_payment:edit"),
        ]]),
    )
    await message.answer("Меню исполнителя", reply_markup=await executor_keyboard(message.from_user.id, state))


@router.callback_query(F.data == "provider_payment:edit")
async def edit_payment_methods(callback: CallbackQuery, state: FSMContext):
    if not isinstance(callback.message, Message) or await state.get_state():
        await callback.answer("Сначала завершите текущее действие.")
        return
    async with aiosqlite.connect(storage.DB_PATH) as db:
        async with db.execute("SELECT 1 FROM provider_profiles WHERE telegram_id=? AND blocked=0",
                              (callback.from_user.id,)) as cursor:
            allowed = await cursor.fetchone()
    if not allowed:
        await callback.answer("Профиль недоступен.")
        return
    await callback.answer()
    await start_payment_selection(callback.message, state, callback.from_user.id)


async def resume_provider_step(message, state, current):
    data = await state.get_data()
    if current == AlertPreferences.select.state:
        await show_alert_preferences(message, state, data["alert_kind"], data.get("alert_group"))
    elif current in (AddMachine.category.state, AddMachine.equipment_type.state):
        await machine_catalog(message, state, data.get("machine_group"))
    elif current == AddMachine.review.state:
        if data.get("machine_choosing_field"):
            await machine_edit_fields(message, state)
        else:
            await review_machine(message, state, data["area"])
    elif current == AddMachine.region.state:
        await ask_machine_region(message, state)
    elif current == Register.payment.state:
        if "payment_selection" in data:
            await show_payment_selection(message, state, set(data["payment_selection"]))
        else:
            await start_payment_selection(message, state, state.key.user_id)
    else:
        prompts = {
            AddMachine.area.state: "Укажите район или введите «весь регион».",
            AddMachine.description.state: "Введите название и описание техники.",
            Register.consent.state: "Сохраняем контакт и сведения о технике для подбора заявок. Согласны? Ответьте «Да».",
            Register.name.state: "Укажите имя или название компании.",
            Register.phone.state: "Укажите телефон в формате +7XXXXXXXXXX или 8XXXXXXXXXX.",
        }
        await message.answer(prompts[current], reply_markup=section_keyboard((HOME,)))
