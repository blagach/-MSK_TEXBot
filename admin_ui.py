"""Private, administrator-only report and export entry points."""
import re
import json
import logging
from aiogram.exceptions import TelegramAPIError
from uuid import uuid4
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import admin_reports as reports
import support_storage
import admin_access
import user_access
import equipment_catalog

router = Router()
router.callback_query.filter(F.message.chat.type == "private")
ADMIN_LABEL = "🛡 Админ-панель"


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=action) for text, action in row]
        for row in rows
    ])


HOME = (("⬅️ В админ-панель", "admin:home"),)


class ManageAccess(StatesGroup):
    choose = State()
    reason = State()
    confirm = State()


class FindLead(StatesGroup):
    query = State()


class FindUser(StatesGroup):
    query = State()
    results = State()


class ManageAdmins(StatesGroup):
    target = State()
    confirm = State()


ROLE_LABELS = {"administrator": "Администратор", "moderator": "Модератор", "support": "Поддержка",
               "finance": "Финансы", "marketer": "Маркетолог", "none": "Без административных прав"}


async def retire_panel(message):
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError as error:
        logging.warning("Could not retire administrator keyboard: %s",type(error).__name__)


async def menu(message, user_id=None, admin_ids=(), owner_id=None):
    user_id = message.chat.id if user_id is None else user_id
    counts=await reports.dashboard_totals(user_id)
    permissions = {key: await admin_access.allowed(user_id,key,admin_ids,owner_id)
                   for key in ("users","catalog","moderate","stats","support","export","roles","audit","finance","marketing")}
    if not any(permissions.values()):
        await message.answer("Нет доступа.")
        return
    pairs = [
        ((f"📊 Статистика · +{counts['registrations_today']}", "admin:stats", "stats"),
         (f"📋 Заявки · {counts['active']} / {counts['pending']}", "admin:records", "moderate")),
        ((f"👥 Пользователи · {counts['users']}", "admin:users", "users"),
         (f"🛟 Поддержка · {counts['support_new']}", "help:queue", "support")),
        ((f"🚜 Каталог техники · {counts['equipment']}", "admin:equipment:0", "catalog"),
         ("💰 Финансы · бесплатно", "admin:finance", "finance")),
        (("📣 Рассылки · выключены", "admin:marketing", "marketing"),
         (f"⚙️ Настройки · {counts['attention']}", "admin:settings", "roles")),
    ]
    rows = [tuple((label, callback) for label, callback, permission in pair if permissions[permission]) for pair in pairs]
    rows = [row for row in rows if row]
    await message.answer("Админ-панель\nЗаявки: активные / непросмотренные. Поддержка: новые.\n"
                         "Статистика: регистрации исполнителей сегодня (Москва). Настройки: публикации и доставки, требующие внимания.",
                         reply_markup=keyboard(*rows))


def audit_line(row):
    actions = {"role_changed": "Изменение прав", "admin_denied": "Отказ в доступе",
               "admin_view": "Просмотр раздела", "lead_view": "Просмотр заявки",
               "lead_publish": "Публикация заявки", "lead_reject": "Отклонение заявки",
               "lead_close": "Закрытие заявки", "support_reply": "Сохранение ответа поддержки",
               "support_delivery": "Отправка ответа поддержки", "user_search": "Поиск пользователей",
               "user_profile": "Просмотр профиля", "user_access": "Изменение доступа",
               "support_status": "Статус обращения", "export_sent": "Выгрузка XLSX", "export_sending": "Начало отправки XLSX", "export_requested": "Запрос XLSX", "export_delivery": "Отправка XLSX"}
    results = {"success": "Успешно", "denied": "Запрещено", "conflict": "Конфликт версий",
               "not_changed": "Без изменения", "accepted": "Запрос принят", "pending": "Ожидается ответ Telegram", "saved": "Сохранено в истории",
               "sent": "Отправку подтвердил Telegram", "failed": "Ошибка отправки",
               "uncertain": "Отправка не подтверждена"}
    text = (f"#{row['id']} · {row['created_at']} (UTC) · пользователь {row['actor_id']}\n"
            f"{actions.get(row['action'], row['action'])} · объект {row['target_id'] or '—'} · "
            f"{results.get(row['result'], row['result'])}")
    try:
        details = json.loads(row['details'])
    except (ValueError, TypeError):
        return text
    if isinstance(details,dict) and row["action"]=="user_access" and row["result"]=="success":
        text += f"\n{scope_text(details.get('previous',[]))} → {scope_text(details.get('scopes',[]))}\nПричина: {str(details.get('reason','—'))[:120]}"
    if isinstance(details,dict) and row["action"]=="export_delivery":
        text += f"\nЭтап: {details.get('stage','sending')} · причина: {details.get('error_code','—')}"
    if isinstance(details, dict) and row['action'] == "support_delivery":
        text += f"\nID ответа: {details.get('message_id')}"
        if details.get("error_code"):
            text += f" · причина: {details['error_code']}"
    if isinstance(details, dict) and row['action'] == "role_changed":
        if row['result'] == "success" and "previous_role" in details:
            text += (f"\n{ROLE_LABELS.get(details['previous_role'], details['previous_role'])} → "
                     f"{ROLE_LABELS.get(details['role'], details['role'])}")
        elif row['result'] == "conflict":
            text += f"\nВерсия подтверждения: {details.get('expected_revision')}; текущая: {details.get('actual_revision')}"
    return text


SCOPE_LABELS = {"requests": "Создание заявок", "offers": "Отправка откликов",
                "notifications": "Уведомления о заявках", "full": "Полное ограничение"}


def scope_text(scopes):
    return ", ".join(SCOPE_LABELS[s] for s in scopes) or "Без новых ограничений"


async def resume_access_step(message, state):
    data = await state.get_data()
    nonce = uuid4().hex[:16]
    await state.update_data(access_nonce=nonce)
    current = await state.get_state()
    cancel = (("Отмена", f"admin:access_cancel:{nonce}"),)
    if current == ManageAccess.choose.state:
        selected = data.get("access_scopes", [])
        rows = [((f"{'✅' if scope in selected else '▫️'} {label}", f"admin:access_pick:{scope}:{nonce}"),)
                for scope,label in SCOPE_LABELS.items()]
        rows += [(("Снять ограничения", f"admin:access_pick:clear:{nonce}"),),
                 (("Продолжить", f"admin:access_next:{nonce}"),), cancel]
        text = f"Доступ пользователя {data['access_target']}\nВыберите ограничения: {scope_text(selected)}"
    elif current == ManageAccess.reason.state:
        text, rows = "Укажите причину изменения доступа (до 500 символов).", [cancel]
    else:
        text = (f"Подтвердите изменение доступа пользователя {data['access_target']}\n"
                f"Было: {scope_text(data['access_previous'])}\nСтанет: {scope_text(data['access_scopes'])}\n"
                f"Причина: {data['access_reason']}\nДанные и история сохраняются. Поддержка доступна.")
        rows = [(("Подтвердить", f"admin:access_confirm:{nonce}"),), cancel]
    sent = await message.answer(text, reply_markup=keyboard(*rows, HOME))
    await state.update_data(form_keyboard_id=sent.message_id)


@router.message(ManageAccess.reason)
async def access_reason(message: Message, state: FSMContext, support_admin_ids, service_owner_id=None):
    if message.chat.id != message.from_user.id or not await admin_access.allowed(
        message.from_user.id,"access",support_admin_ids,service_owner_id,
    ):
        await message.answer("Нет доступа.")
        return
    reason = (message.text or "").strip()
    if not 1 <= len(reason) <= 500:
        await message.answer("Укажите причину длиной от 1 до 500 символов.")
        return
    await state.update_data(access_reason=reason)
    await state.set_state(ManageAccess.confirm)
    await resume_access_step(message,state)


async def resume_lead_search(message,state):
    data=await state.get_data()
    last=f"\nПоследний номер: {data['lead_search_id']}" if data.get('lead_search_id') else ""
    sent=await message.answer("Введите номер заявки, например 11 или №11."+last,reply_markup=keyboard(HOME))
    await state.update_data(form_keyboard_id=sent.message_id)


@router.message(FindLead.query)
async def find_lead(message: Message,state: FSMContext,support_admin_ids,service_owner_id=None):
    if message.chat.id!=message.from_user.id or not await admin_access.allowed(
        message.from_user.id,'moderate',support_admin_ids,service_owner_id,
    ):
        await message.answer('Нет доступа.')
        return
    value=(message.text or '').strip().lstrip('#№').strip()
    if not re.fullmatch(r'[1-9][0-9]{0,17}',value):
        await message.answer('Введите положительный номер заявки до 18 цифр.',reply_markup=keyboard(HOME))
        return
    await state.update_data(lead_search_id=int(value))
    from bot import get_lead,show_customer_lead
    lead=await get_lead(int(value))
    if lead is None:
        await message.answer('Заявка не найдена. Можно ввести другой номер.',reply_markup=keyboard(HOME))
        return
    if not await admin_access.allowed(message.from_user.id,'moderate',support_admin_ids,service_owner_id):
        await message.answer('Право просмотра изменилось. Карточка не отправлена.')
        return
    await show_customer_lead(message,lead,message.from_user.id==lead['customer_id'],True,admin_back='admin:records')
    await admin_access.mark_lead_view(message.from_user.id,lead['id'])
    await admin_access.record(message.from_user.id,'lead_view',target_id=lead['id'],details={'source':'id_search'})


async def show_people(message, state, *, kind="all", after=0, query="", nonce=None):
    nonce = nonce or uuid4().hex[:16]
    await state.set_state(FindUser.results)
    await state.update_data(user_query=query, user_kind=kind, user_nonce=nonce, user_after=after)
    page = await reports.user_page(query, kind, after, 11)
    buttons = [((f"{row['id']} · {(row['display_name'] or 'Без имени')[:35]}", f"admin:user:{row['id']}"),)
               for row in page[:10]]
    if len(page) > 10:
        buttons.append((("Далее", f"admin:people:{page[9]['id']}:{nonce}"),))
    buttons.extend([(("🔎 Новый поиск", "admin:find"),), (("К пользователям", "admin:users"),), HOME])
    sent = await message.answer("Пользователи" + (f" · поиск: {query}" if query else "") +
                               ("\nНичего не найдено." if not page else ""), reply_markup=keyboard(*buttons))
    await state.update_data(form_keyboard_id=sent.message_id)


async def resume_user_search(message, state):
    if await state.get_state() == FindUser.query.state:
        sent = await message.answer("Введите имя, Telegram ID, username или полный телефон.", reply_markup=keyboard(HOME))
        await state.update_data(form_keyboard_id=sent.message_id)
    else:
        data = await state.get_data()
        await show_people(message, state, kind=data.get("user_kind", "all"),
                          query=data.get("user_query", ""), after=data.get("user_after", 0))


@router.message(FindUser.query)
async def find_user(message: Message, state: FSMContext, support_admin_ids, service_owner_id=None):
    if message.chat.id != message.from_user.id or not await admin_access.allowed(
        message.from_user.id, "users", support_admin_ids, service_owner_id,
    ):
        await message.answer("Нет доступа.")
        return
    query = (message.text or "").strip()
    if not 1 <= len(query) <= 100:
        await message.answer("Введите от 1 до 100 символов для поиска.")
        return
    await admin_access.record(message.from_user.id, "user_search")
    await show_people(message, state, query=query)


async def resume_admin_step(message, state):
    data = await state.get_data()
    if await state.get_state() == ManageAdmins.target.state:
        await message.answer("Введите числовой Telegram ID пользователя.", reply_markup=keyboard(HOME))
        return
    if "admin_expected_revision" not in data:
        await state.set_state(ManageAdmins.target)
        await message.answer("Для безопасности проверьте права заново: введите Telegram ID пользователя.",
                             reply_markup=keyboard(HOME))
        return
    nonce = uuid4().hex
    await state.update_data(admin_nonce=nonce)
    sent = await message.answer(
        f"Изменить права пользователя {data['admin_target']}?\n"
        f"Текущая роль: {ROLE_LABELS.get(data.get('admin_previous_role'), data.get('admin_previous_role', '—'))}\n"
        f"Новая роль: {ROLE_LABELS[data['admin_role']]}",
        reply_markup=keyboard((("Подтвердить", f"admin:confirm:{nonce}"), ("Отмена", f"admin:role_cancel:{nonce}"))),
    )
    await state.update_data(form_keyboard_id=sent.message_id)


@router.message(ManageAdmins.target)
async def admin_target(message: Message, state: FSMContext, support_admin_ids, service_owner_id=None):
    if message.chat.id != message.from_user.id or not await admin_access.allowed(
        message.from_user.id, "roles", support_admin_ids, service_owner_id,
    ):
        await message.answer("Нет доступа.")
        return
    if not re.fullmatch(r"[1-9][0-9]{0,18}", message.text or ""):
        await message.answer("Введите корректный числовой Telegram ID.")
        return
    if int(message.text) == service_owner_id:
        await message.answer("Роль владельца задаётся конфигурацией и здесь не изменяется.")
        return
    snapshot = await admin_access.role_snapshot(int(message.text), support_admin_ids, service_owner_id)
    await state.update_data(admin_target=int(message.text), admin_expected_revision=snapshot["revision"],
                            admin_previous_role=snapshot["role"])
    await state.set_state(ManageAdmins.confirm)
    await resume_admin_step(message, state)


@router.callback_query(F.data.startswith("admin:"))
async def admin_action(callback: CallbackQuery, support_admin_ids, state: FSMContext = None, service_owner_id=None):
    message = callback.message
    if (
        not isinstance(message, Message)
        or message.chat.type != "private" or message.chat.id != callback.from_user.id
    ):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    action = callback.data.removeprefix("admin:")
    permission = {"home": "panel", "users": "users", "records": "moderate", "stats": "stats",
                  "export": "export", "settings": "roles", "audit": "audit", "finance": "finance",
                  "marketing": "marketing", "notification_settings": "roles", "service_settings": "roles"}.get(action)
    if action.startswith(("access:", "access_pick:", "access_next:", "access_cancel:", "access_confirm:")):
        permission = "access"
    if action.startswith("leads:") or action=="lead_find":
        permission = "moderate"
    if action.startswith(("people:", "user:", "users_filter:")) or action == "find":
        permission = "users"
    if action.startswith(("equipment:", "catalog:")):
        permission = "catalog"
    if re.fullmatch(r"audit_filter:(all|permissions|errors|exports):[0-9]{1,19}",action):
        permission = "audit"
    if re.fullmatch(r"audit:[1-9][0-9]{0,18}", action):
        permission = "audit"
    if re.fullmatch(r"admins:[0-9]{1,19}", action):
        permission = "roles"
    if action.startswith(("role:", "confirm:", "role_cancel:")):
        permission = "roles"
    if permission is None or not await admin_access.allowed(
        callback.from_user.id, permission, support_admin_ids, service_owner_id,
    ):
        await admin_access.record(callback.from_user.id, "admin_denied", result="denied")
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    await admin_access.record(callback.from_user.id, "admin_view", details={"section": permission})
    if action == "home":
        if state is not None and await state.get_state() in (*ManageAdmins.__all_states_names__, *FindUser.__all_states_names__, *ManageAccess.__all_states_names__, *FindLead.__all_states_names__):
            from bot import pause_form
            await pause_form(message, state)
        await menu(message, callback.from_user.id, support_admin_ids, service_owner_id)
    elif action == "settings":
        await message.answer("Настройки", reply_markup=keyboard(
            (("Администраторы", "admin:admins:0"), ("Уведомления", "admin:notification_settings")),
            (("Параметры сервиса", "admin:service_settings"), ("Журнал действий", "admin:audit")), HOME,
        ))
    elif action == "notification_settings":
        moderation = await admin_access.staff("moderate",support_admin_ids,service_owner_id)
        support = await admin_access.staff("support",support_admin_ids,service_owner_id)
        await message.answer(f"Служебные уведомления\nМодерация: получателей {len(moderation)}\n"
                             f"Поддержка: получателей {len(support)}\n"
                             "Получатели определяются актуальными правами. Настройки исполнителей сохраняются отдельно.",
                             reply_markup=keyboard(HOME))
    elif action == "service_settings":
        await message.answer("Параметры сервиса\nБесплатный режим. Реальные платежи не подключены.\n"
                             f"Общий каталог: {len(equipment_catalog.GROUPS)} групп, {len(equipment_catalog.TYPES)} позиций.\n"
                             "Смена платного режима и отправка рассылок здесь пока недоступны.",reply_markup=keyboard(HOME))
    elif action == "finance":
        await message.answer("Финансы\nСервис бесплатный. Реальные платежи не подключены.\n"
                             "Финансовый учёт, тарифы, платёжная песочница и возвраты ещё требуют реализации; "
                             "этот экран не принимает оплату и не изменяет доступ пользователей.",reply_markup=keyboard(HOME))
    elif action == "marketing":
        await message.answer("Рассылки\nОтправка отключена. Сообщения пользователям не отправляются.\n"
                             "Кампании, согласия, расписание и подтверждение владельцем требуют отдельной реализации "
                             "после сверки полного маркетингового ТЗ-6.",reply_markup=keyboard(HOME))
    elif action.startswith("admins:"):
        after = min(int(action.split(":")[1]), 2**63 - 1)
        page = await admin_access.administrator_page(support_admin_ids, service_owner_id, after=after, limit=11)
        rows = page[:10]
        labels = {**ROLE_LABELS, "owner": "Владелец", "finance": "Финансы", "marketer": "Маркетолог"}
        text = "Права администраторов\n\n" + "\n".join(
            f"ID {row['id']} · {labels.get(row['role'], row['role'])}" for row in rows
        )
        buttons = [(("Назначить права", "admin:role:choose"),)]
        if len(page) > 10:
            buttons.append((("Далее", f"admin:admins:{rows[-1]['id']}"),))
        if after:
            buttons.append((("К началу списка", "admin:admins:0"),))
        buttons.append(HOME)
        await message.answer(text if rows else "Права администраторов: записей на этой странице нет.",
                             reply_markup=keyboard(*buttons))
    elif action == "role:choose":
        await message.answer("Выберите роль для назначения. Изменение потребует подтверждения.",
                             reply_markup=keyboard(*(((label, f"admin:role:{role}"),)
                                                       for role, label in ROLE_LABELS.items()), HOME))
    elif action.startswith("role:") and action[5:] in ROLE_LABELS and state is not None:
        if await state.get_state() is not None:
            await message.answer("Сначала завершите открытое действие или перейдите в главное меню.")
            return
        await state.set_state(ManageAdmins.target)
        await state.set_data({"admin_role": action[5:]})
        await resume_admin_step(message, state)
    elif action.startswith("role_cancel:") and state is not None:
        data = await state.get_data()
        if (await state.get_state() != ManageAdmins.confirm.state or action[12:] != data.get("admin_nonce")
                or message.message_id != data.get("form_keyboard_id")):
            await message.answer("Этот экран устарел. Откройте назначение прав снова.")
            return
        await state.clear()
        await retire_panel(message)
        await message.answer("Изменение прав отменено.", reply_markup=keyboard(HOME))
    elif action.startswith("confirm:") and state is not None:
        data = await state.get_data()
        if (await state.get_state() != ManageAdmins.confirm.state or action[8:] != data.get("admin_nonce")
                or message.message_id != data.get("form_keyboard_id")):
            await message.answer("Этот экран устарел. Откройте назначение прав снова.")
            return
        if "admin_expected_revision" not in data:
            await state.set_state(ManageAdmins.target)
            await retire_panel(message)
            await message.answer("Введите Telegram ID заново для проверки актуальных прав.", reply_markup=keyboard(HOME))
            return
        try:
            changed = await admin_access.assign_role(
                callback.from_user.id, data["admin_target"], data["admin_role"], data["admin_nonce"],
                owner_id=service_owner_id, expected_revision=data["admin_expected_revision"],
                legacy_admins=support_admin_ids,
            )
        except ValueError as error:
            await message.answer(str(error),reply_markup=keyboard(HOME))
            return
        except admin_access.RoleConflict:
            await state.set_state(ManageAdmins.target)
            await retire_panel(message)
            await message.answer("Права уже изменились. Введите Telegram ID заново и проверьте текущую роль.",
                                 reply_markup=keyboard(HOME))
            return
        await state.clear()
        await retire_panel(message)
        if changed:
            text = "Права сохранены."
        else:
            snapshot = await admin_access.role_snapshot(data["admin_target"], support_admin_ids, service_owner_id)
            text = f"Изменение уже обработано. Текущая роль: {ROLE_LABELS.get(snapshot['role'], snapshot['role'])}"
        await message.answer(text, reply_markup=keyboard(HOME))
    elif action == "audit" or action.startswith(("audit:","audit_filter:")):
        kind="all"
        if action.startswith("audit_filter:"):
            _,kind,cursor=action.split(":")
            before=min(int(cursor),2**63-1) if int(cursor) else 2**63-1
        else:
            before=min(int(action.split(":")[1]),2**63-1) if ":" in action else 2**63-1
        page = await admin_access.history(before,11,kind=kind)
        rows = page[:10]
        text = "\n\n".join(audit_line(row) for row in rows)
        navigation = []
        if len(page) > 10:
            target=f"admin:audit:{rows[-1]['id']}" if kind=="all" else f"admin:audit_filter:{kind}:{rows[-1]['id']}"
            navigation.append((("Ранее",target),))
        navigation.extend([
            (("Все","admin:audit_filter:all:0"),("Права и ограничения","admin:audit_filter:permissions:0")),
            (("Ошибки","admin:audit_filter:errors:0"),("Выгрузки","admin:audit_filter:exports:0")),HOME,
        ])
        await message.answer(text or "Журнал пока пуст.", reply_markup=keyboard(*navigation))
    elif action == "users":
        data = await reports.user_totals()
        await message.answer(
            f"Всего известных пользователей · {data['total']}\n"
            f"Заказчики · {data['customers']}\nИсполнители · {data['providers']}\n"
            f"Две роли · {data['both']}\n\n"
            "Общий итог включает зафиксированные посещения. Старые посещения без действий могли не сохраниться.\n"
            "Заказчики учитываются по заявкам, исполнители по регистрации или отклику.",
            reply_markup=keyboard((("🔎 Поиск", "admin:find"), ("Все", "admin:users_filter:all")),
                                  (("Заказчики", "admin:users_filter:customers"), ("Исполнители", "admin:users_filter:providers")),
                                  (("Ограниченные", "admin:users_filter:restricted"),),
                                  *([(("📎 Выгрузка XLSX", "admin:export"),)] if await admin_access.allowed(
                                      callback.from_user.id,"export",support_admin_ids,service_owner_id) else []), HOME),
        )
    elif action.startswith("access:") and state is not None:
        target = action.split(":",1)[1]
        if not re.fullmatch(r"[1-9][0-9]{0,17}",target) or await reports.user_profile(int(target)) is None:
            await message.answer("Пользователь не найден.")
            return
        if await state.get_state() not in (None,*FindUser.__all_states_names__):
            await message.answer("Сначала завершите текущее действие или перейдите в меню.")
            return
        from bot import pause_form
        await pause_form(message,state)
        saved = await user_access.snapshot(int(target))
        await state.set_state(ManageAccess.choose)
        await state.set_data({"access_target":int(target),"access_previous":saved["scopes"],
                              "access_scopes":saved["scopes"],"access_revision":saved["revision"]})
        await resume_access_step(message,state)
    elif action.startswith(("access_pick:","access_next:","access_cancel:","access_confirm:")) and state is not None:
        parts = action.split(":")
        data = await state.get_data()
        current = await state.get_state()
        if (current not in ManageAccess.__all_states_names__ or parts[-1] != data.get("access_nonce")
                or message.message_id != data.get("form_keyboard_id")):
            await message.answer("Этот экран доступа устарел. Продолжите действие через меню.")
            return
        if parts[0] == "access_cancel":
            await state.clear()
            await retire_panel(message)
            await message.answer("Изменение доступа отменено.",reply_markup=keyboard(HOME))
            return
        if parts[0] == "access_pick" and current == ManageAccess.choose.state and len(parts)==3:
            scope = parts[1]
            selected = set(data["access_scopes"])
            if scope == "clear":
                selected = set()
            elif scope in SCOPE_LABELS:
                if scope in selected:
                    selected.remove(scope)
                elif scope == "full":
                    selected = {"full"}
                else:
                    selected.discard("full")
                    selected.add(scope)
            else:
                return
            await state.update_data(access_scopes=sorted(selected))
        elif parts[0] == "access_next" and current == ManageAccess.choose.state:
            await state.set_state(ManageAccess.reason)
        elif parts[0] == "access_confirm" and current == ManageAccess.confirm.state:
            try:
                changed = await user_access.change(callback.from_user.id,data["access_target"],data["access_scopes"],
                    data["access_reason"],data["access_nonce"],expected_revision=data["access_revision"],
                    owner_id=service_owner_id,legacy_admins=support_admin_ids)
            except user_access.AccessConflict:
                saved = await user_access.snapshot(data["access_target"])
                await state.update_data(access_previous=saved["scopes"],access_scopes=saved["scopes"],access_revision=saved["revision"])
                await state.set_state(ManageAccess.choose)
                await message.answer("Ограничения уже изменились. Проверьте их заново.")
            except ValueError as error:
                await message.answer(str(error),reply_markup=keyboard(HOME))
                return
            else:
                await state.clear()
                await retire_panel(message)
                await message.answer("Изменение доступа сохранено." if changed else "Подтверждение уже обработано.",
                                     reply_markup=keyboard(HOME))
                return
        else:
            return
        await retire_panel(message)
        await resume_access_step(message,state)
    elif action == "find" and state is not None:
        if await state.get_state() not in (None, *FindUser.__all_states_names__):
            await message.answer("Сначала завершите текущее действие или перейдите в главное меню.")
            return
        await state.set_state(FindUser.query)
        await resume_user_search(message, state)
    elif action.startswith("users_filter:") and state is not None:
        kind = action.split(":", 1)[1]
        if kind not in {"all", "customers", "providers", "restricted"}:
            return
        if await state.get_state() not in (None, *FindUser.__all_states_names__):
            await message.answer("Сначала завершите текущее действие или перейдите в главное меню.")
            return
        await show_people(message, state, kind=kind)
    elif action.startswith("people:") and state is not None:
        parts = action.split(":")
        data = await state.get_data()
        if (len(parts) != 3 or not re.fullmatch(r"[1-9][0-9]{0,17}", parts[1])
                or await state.get_state() != FindUser.results.state or parts[2] != data.get("user_nonce")
                or message.message_id != data.get("form_keyboard_id")):
            await message.answer("Этот поиск устарел. Откройте список заново.")
            return
        await show_people(message, state, kind=data["user_kind"], query=data["user_query"],
                          after=int(parts[1]), nonce=data["user_nonce"])
    elif re.fullmatch(r"user:[1-9][0-9]{0,17}", action):
        profile = await reports.user_profile(int(action[5:]))
        if profile is None:
            await message.answer("Пользователь не найден.", reply_markup=keyboard(HOME))
            return
        await admin_access.record(callback.from_user.id, "user_profile", target_id=profile["id"])
        roles = ", ".join(label for key, label in (("customer", "Заказчик"), ("performer", "Исполнитель")) if profile[key])
        await message.answer(
            f"Пользователь ID {profile['id']}\nИмя: {(profile['display_name'] or 'Не указано')[:300]}\n"
            f"Роли: {roles or 'Посетитель'}\nТелефон: {profile['phone'] or 'Не указан'}\n"
            f"Telegram: {reports.profile_url(profile['username']) or 'Не указан'}\n"
            f"Заявки: {profile['leads']} · Отклики: {profile['offers']}\n"
            f"Техника: {profile['equipment']} · Обращения: {profile['tickets']}",
            reply_markup=keyboard(*([(("Управление доступом", f"admin:access:{profile['id']}"),)]
                                  if await admin_access.allowed(callback.from_user.id,"access",support_admin_ids,service_owner_id) else []),
                                  (("К пользователям", "admin:users"),), HOME),
        )
        saved = await user_access.snapshot(profile["id"])
        await message.answer("Ограничения: " + scope_text(saved["scopes"]) +
                             ("\nПричина последнего изменения: " + saved["reason"] if saved["reason"] else "") +
                             ("\nТакже действует прежняя блокировка профиля исполнителя; она здесь не снимается."
                              if profile["legacy_blocked"] else ""))
    elif action == "records":
        leads, offers, delivery, results = await reports.lead_and_offer_counts()
        lead_counts = " · ".join(f"{label} {n}" for label, n in reports.classify(leads).items())
        offer_counts = " · ".join(f"{label} {n}" for label, n in reports.classify(offers).items())
        labels = {"sent": "отправлен", "pending": "ожидается", "unconfirmed": "не подтверждена"}
        deliveries = " · ".join(f"{labels.get(r['status'], r['status'])} {r['n']}" for r in delivery)
        outcomes = " · ".join(f"{row['status']} {row['n']}" for row in results)
        await message.answer(
            f"Заявки\n{lead_counts}\n\nОтклики по статусу связанной заявки\n{offer_counts}\n\n"
            f"Результаты откликов\n{outcomes or 'Нет записей'}\n\n"
            f"Доставка откликов\n{deliveries or 'Нет записей'}",
            reply_markup=keyboard((("Новые", "admin:leads:new:0"), ("Активные", "admin:leads:active:0")),
                                  (("Закрытые", "admin:leads:closed:0"), ("Все", "admin:leads:all:0")),
                                  (("🔎 Поиск по номеру", "admin:lead_find"),), HOME),
        )
    elif action == 'lead_find' and state is not None:
        if await state.get_state() not in (None,*FindLead.__all_states_names__):
            await message.answer('Сначала завершите текущее действие или перейдите в главное меню.')
            return
        await state.set_state(FindLead.query)
        await resume_lead_search(message,state)
    elif action.startswith("leads:"):
        parts=action.split(":")
        if len(parts)!=3 or parts[1] not in {'new','active','closed','all'} or not re.fullmatch(r"[0-9]{1,18}",parts[2]):
            return
        kind,before=parts[1],int(parts[2])
        rows=await reports.administrator_leads(callback.from_user.id,kind,before or 2**63-1,11)
        titles={'new':'Новые для вас','active':'Активные','closed':'Закрытые','all':'Все'}
        buttons=[((f"№{row['id']} · {(row['equipment'] or 'Техника не указана')[:35]}",f"lc:open:{row['id']}:a-{kind}-{before}"),) for row in rows[:10]]
        if len(rows)>10:
            buttons.append((("Далее",f"admin:leads:{kind}:{rows[9]['id']}"),))
        buttons.extend([(("К спискам заявок","admin:records"),),HOME])
        await message.answer(titles[kind]+("\nЗаписей нет." if not rows else ""),reply_markup=keyboard(*buttons))
    elif action == "stats":
        result = await reports.registration_stats()
        lines = [
            f"{period} · регистраций {row['providers']}, первых заявок {row['customers']}, "
            f"создано {row['created']}, опубликовано {row['published']}, откликов {row['offers']}"
            for period, row in zip(("День", "Неделя", "Месяц"), result)
        ]
        await message.answer("Календарные периоды по Москве. Публикации учитываются с момента включения журнала.\n\n" + "\n".join(lines), reply_markup=keyboard(HOME))
    elif action == "export":
        request_id=getattr(callback,"id",None)
        try:
            fresh=await admin_access.reserve_export(callback.from_user.id,request_id)
        except ValueError:
            await message.answer("Не удалось подтвердить запрос выгрузки. Откройте раздел заново.")
            return
        if not fresh:
            await message.answer("Этот запрос уже обрабатывался. Повторная отправка не выполнялась.",reply_markup=keyboard(HOME))
            return
        try:
            content=await reports.export_xlsx()
        except Exception as error:
            await admin_access.record(callback.from_user.id,"export_delivery",result="failed",
                                      details={"request_id":request_id,"stage":"generation","error_code":type(error).__name__})
            await message.answer("Не удалось подготовить файл. Повторная отправка не выполнялась.",reply_markup=keyboard(HOME))
            return
        document = BufferedInputFile(content, filename="msk-texbot-admin.xlsx")
        if not await admin_access.allowed(callback.from_user.id,"export",support_admin_ids,service_owner_id):
            await admin_access.record(callback.from_user.id,"admin_denied",result="denied",details={"section":"export","stage":"before_send","request_id":request_id})
            await message.answer("Право на выгрузку изменилось. Файл не отправлен.")
            return
        await admin_access.record(callback.from_user.id,"export_sending",result="pending",details={"request_id":request_id})
        try:
            await message.answer_document(document, caption="Служебная выгрузка MSK TEXBot.", reply_markup=keyboard(HOME))
        except (TelegramAPIError,TimeoutError) as error:
            from aiogram.exceptions import TelegramBadRequest,TelegramForbiddenError,TelegramNotFound,TelegramRetryAfter
            status="failed" if isinstance(error,(TelegramBadRequest,TelegramForbiddenError,TelegramNotFound,TelegramRetryAfter)) else "uncertain"
            await admin_access.record(callback.from_user.id,"export_delivery",result=status,
                                      details={"error_code":type(error).__name__,"request_id":request_id,"stage":"sending"})
            await message.answer("Отправка файла не подтверждена." if status=="uncertain" else "Файл не отправлен: Telegram отклонил запрос.",
                                 reply_markup=keyboard(HOME))
            return
        await admin_access.record(callback.from_user.id, "export_sent",details={"request_id":request_id})
    elif re.fullmatch(r"equipment:[0-9]{1,7}", action):
        totals = await reports.equipment_totals()
        await message.answer(f"Зарегистрированные исполнители · {totals['providers']}\nЕдиницы техники · {totals['units']}",
                             reply_markup=keyboard((("Справочник техники", "admin:catalog:groups"),), HOME))
    elif action == "catalog:groups":
        buttons = [(title, f"admin:catalog:{index}") for index,(title,_) in enumerate(equipment_catalog.GROUPS)]
        await message.answer("Единый каталог заказчика и исполнителя",reply_markup=keyboard(
            *(tuple(buttons[i:i+2]) for i in range(0,len(buttons),2)),HOME))
    elif re.fullmatch(r"catalog:[0-9]",action):
        title,items=equipment_catalog.GROUPS[int(action.split(":")[1])]
        await message.answer(title+"\n\n"+"\n".join(items),
                             reply_markup=keyboard((("К категориям", "admin:catalog:groups"),),HOME))
