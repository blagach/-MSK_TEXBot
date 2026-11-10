"""Private, administrator-only report and export entry points."""
import re

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import admin_reports as reports
import support_storage

router = Router()
router.callback_query.filter(F.message.chat.type == "private")
ADMIN_LABEL = "🛡 Админ-панель"


def keyboard(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=action) for text, action in row]
        for row in rows
    ])


HOME = (("⬅️ В админ-панель", "admin:home"),)


async def menu(message):
    totals = await support_storage.counts()
    await message.answer("Админ-панель", reply_markup=keyboard(
        (("👥 Пользователи", "admin:users"), ("🚜 Исполнители и техника", "admin:equipment:0")),
        (("📋 Заявки и отклики", "admin:records"), ("📊 Статистика", "admin:stats")),
        ((f"📥 Обращения · {totals.get('new', 0)}", "help:queue"), ("📎 Выгрузка XLSX", "admin:export")),
        (("📝 Модерация заявок", "lc:admin:0"),),
    ))


@router.callback_query(F.data.startswith("admin:"))
async def admin_action(callback: CallbackQuery, support_admin_ids):
    message = callback.message
    if (
        callback.from_user.id not in support_admin_ids or not isinstance(message, Message)
        or message.chat.type != "private" or message.chat.id != callback.from_user.id
    ):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await callback.answer()
    action = callback.data.removeprefix("admin:")
    if action == "home":
        await menu(message)
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
        leads, offers, delivery = await reports.lead_and_offer_counts()
        lead_counts = " · ".join(f"{label} {n}" for label, n in reports.classify(leads).items())
        offer_counts = " · ".join(f"{label} {n}" for label, n in reports.classify(offers).items())
        labels = {"sent": "отправлен", "pending": "ожидается", "unconfirmed": "не подтверждена"}
        deliveries = " · ".join(f"{labels.get(r['status'], r['status'])} {r['n']}" for r in delivery)
        await message.answer(
            f"Заявки\n{lead_counts}\n\nОтклики по статусу связанной заявки\n{offer_counts}\n\n"
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
    elif re.fullmatch(r"equipment:[0-9]{1,7}", action):
        totals = await reports.equipment_totals()
        await message.answer(f"Зарегистрированные исполнители · {totals['providers']}\nЕдиницы техники · {totals['units']}",
                             reply_markup=keyboard(HOME))
