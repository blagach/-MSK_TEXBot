"""Private, administrator-only report and export entry points."""
import re
import json
from uuid import uuid4
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import admin_reports as reports
import support_storage
import admin_access

router = Router()
router.callback_query.filter(F.message.chat.type == "private")
ADMIN_LABEL = "🛡 Админ-панель"


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=action) for text, action in row]
        for row in rows
    ])


HOME = (("⬅️ В админ-панель", "admin:home"),)


class ManageAdmins(StatesGroup):
    target = State()
    confirm = State()


ROLE_LABELS = {"administrator": "Администратор", "moderator": "Модератор", "support": "Поддержка",
               "none": "Без административных прав"}


async def menu(message, user_id=None, admin_ids=(), owner_id=None):
    user_id = message.chat.id if user_id is None else user_id
    permissions = {key: await admin_access.allowed(user_id, key, admin_ids, owner_id)
                   for key in ("users", "catalog", "moderate", "stats", "support", "export", "roles", "audit")}
    totals = await support_storage.counts()
    async with reports.read_db() as db:
        counts = (await reports.rows(db, """SELECT
            (SELECT count(*) FROM leads WHERE status IN ('new','published')) AS active,
            (SELECT count(*) FROM leads l WHERE status IN ('new','published')
             AND NOT EXISTS(SELECT 1 FROM admin_lead_views v WHERE v.admin_id=? AND v.lead_id=l.id)) AS pending,
            (SELECT count(*) FROM offers WHERE delivery_status='unconfirmed')
             + (SELECT count(*) FROM support_reply_deliveries WHERE status IN ('failed','uncertain')) AS attention
        """, (user_id,)))[0]
    pairs = [
        (("👥 Пользователи", "admin:users", "users"), ("🚜 Исполнители и техника", "admin:equipment:0", "catalog")),
        ((f"📋 Заявки и отклики · активных {counts['active']}", "admin:records", "moderate"),
         ("📊 Статистика", "admin:stats", "stats")),
        ((f"📥 Обращения · {totals.get('new', 0)}", "help:queue", "support"), ("📎 Выгрузка XLSX", "admin:export", "export")),
        ((f"📝 Модерация заявок · новых {counts['pending']}", "lc:admin:0", "moderate"),),
        ((f"⚙️ Настройки · внимания {counts['attention']}", "admin:settings", "roles"),),
    ]
    rows = [tuple((label, callback) for label, callback, permission in pair if permissions[permission]) for pair in pairs]
    rows = [row for row in rows if row]
    await message.answer("Админ-панель", reply_markup=keyboard(*rows))


def audit_line(row):
    actions = {"role_changed": "Изменение прав", "admin_denied": "Отказ в доступе",
               "admin_view": "Просмотр раздела", "lead_view": "Просмотр заявки",
               "lead_publish": "Публикация заявки", "lead_reject": "Отклонение заявки",
               "lead_close": "Закрытие заявки", "support_reply": "Сохранение ответа поддержки",
               "support_delivery": "Отправка ответа поддержки",
               "support_status": "Статус обращения", "export_sent": "Выгрузка XLSX"}
    results = {"success": "Успешно", "denied": "Запрещено", "conflict": "Конфликт версий",
               "not_changed": "Без изменения", "saved": "Сохранено в истории",
               "sent": "Отправку подтвердил Telegram", "failed": "Ошибка отправки",
               "uncertain": "Отправка не подтверждена"}
    text = (f"#{row['id']} · {row['created_at']} (UTC) · пользователь {row['actor_id']}\n"
            f"{actions.get(row['action'], row['action'])} · объект {row['target_id'] or '—'} · "
            f"{results.get(row['result'], row['result'])}")
    try:
        details = json.loads(row['details'])
    except (ValueError, TypeError):
        return text
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
                  "export": "export", "settings": "roles", "audit": "audit"}.get(action)
    if action.startswith("equipment:"):
        permission = "catalog"
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
        if state is not None and await state.get_state() in ManageAdmins.__all_states_names__:
            from bot import pause_form
            await pause_form(message, state)
        await menu(message, callback.from_user.id, support_admin_ids, service_owner_id)
    elif action == "settings":
        await message.answer("Настройки", reply_markup=keyboard(
            (("Администраторы", "admin:admins:0"), ("Журнал действий", "admin:audit")), HOME,
        ))
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
        await message.edit_reply_markup(reply_markup=None)
        await message.answer("Изменение прав отменено.", reply_markup=keyboard(HOME))
    elif action.startswith("confirm:") and state is not None:
        data = await state.get_data()
        if (await state.get_state() != ManageAdmins.confirm.state or action[8:] != data.get("admin_nonce")
                or message.message_id != data.get("form_keyboard_id")):
            await message.answer("Этот экран устарел. Откройте назначение прав снова.")
            return
        if "admin_expected_revision" not in data:
            await state.set_state(ManageAdmins.target)
            await message.edit_reply_markup(reply_markup=None)
            await message.answer("Введите Telegram ID заново для проверки актуальных прав.", reply_markup=keyboard(HOME))
            return
        try:
            changed = await admin_access.assign_role(
                callback.from_user.id, data["admin_target"], data["admin_role"], data["admin_nonce"],
                owner_id=service_owner_id, expected_revision=data["admin_expected_revision"],
                legacy_admins=support_admin_ids,
            )
        except admin_access.RoleConflict:
            await state.set_state(ManageAdmins.target)
            await message.edit_reply_markup(reply_markup=None)
            await message.answer("Права уже изменились. Введите Telegram ID заново и проверьте текущую роль.",
                                 reply_markup=keyboard(HOME))
            return
        await state.clear()
        await message.edit_reply_markup(reply_markup=None)
        if changed:
            text = "Права сохранены."
        else:
            snapshot = await admin_access.role_snapshot(data["admin_target"], support_admin_ids, service_owner_id)
            text = f"Изменение уже обработано. Текущая роль: {ROLE_LABELS.get(snapshot['role'], snapshot['role'])}"
        await message.answer(text, reply_markup=keyboard(HOME))
    elif action == "audit" or action.startswith("audit:"):
        before = min(int(action.split(":")[1]), 2**63 - 1) if ":" in action else 2**63 - 1
        page = await admin_access.history(before, 11)
        rows = page[:10]
        text = "\n\n".join(audit_line(row) for row in rows)
        navigation = []
        if len(page) > 10:
            navigation.append((("Ранее", f"admin:audit:{rows[-1]['id']}"),))
        navigation.append(HOME)
        await message.answer(text or "Журнал пока пуст.", reply_markup=keyboard(*navigation))
    elif action == "users":
        data = await reports.user_totals()
        await message.answer(
            f"Всего известных пользователей · {data['total']}\n"
            f"Заказчики · {data['customers']}\nИсполнители · {data['providers']}\n"
            f"Две роли · {data['both']}\n\n"
            "Общий итог включает зафиксированные посещения. Старые посещения без действий могли не сохраниться.\n"
            "Заказчики учитываются по заявкам, исполнители по регистрации или отклику.",
            reply_markup=keyboard(HOME),
        )
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
            f"Доставка откликов\n{deliveries or 'Нет записей'}", reply_markup=keyboard(HOME),
        )
    elif action == "stats":
        result = await reports.registration_stats()
        lines = [
            f"{period} · регистраций {row['providers']}, первых заявок {row['customers']}, "
            f"создано {row['created']}, опубликовано {row['published']}, откликов {row['offers']}"
            for period, row in zip(("День", "Неделя", "Месяц"), result)
        ]
        await message.answer("Календарные периоды по Москве. Публикации учитываются с момента включения журнала.\n\n" + "\n".join(lines), reply_markup=keyboard(HOME))
    elif action == "export":
        document = BufferedInputFile(await reports.export_xlsx(), filename="msk-texbot-admin.xlsx")
        await message.answer_document(document, caption="Служебная выгрузка MSK TEXBot.", reply_markup=keyboard(HOME))
        await admin_access.record(callback.from_user.id, "export_sent")
    elif re.fullmatch(r"equipment:[0-9]{1,7}", action):
        totals = await reports.equipment_totals()
        await message.answer(f"Зарегистрированные исполнители · {totals['providers']}\nЕдиницы техники · {totals['units']}",
                             reply_markup=keyboard(HOME))
