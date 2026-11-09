"""Read-only operations and export for the administrator panel."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

import storage

USERS = """
WITH customers AS (
    SELECT customer_id AS id, MIN(created_at) AS first_seen FROM leads GROUP BY customer_id
), registrations AS (
    SELECT id, MIN(registered_at) AS first_seen FROM (
        SELECT telegram_id AS id, consent_at AS registered_at FROM provider_profiles
        UNION ALL SELECT telegram_id, created_at FROM providers
    ) GROUP BY id
), performers AS (
    SELECT id FROM registrations UNION SELECT provider_id FROM offers
), users AS (
    SELECT id FROM customers UNION SELECT id FROM performers
)
"""
EQUIPMENT = """
SELECT * FROM (
    SELECT CAST(e.id AS TEXT) AS equipment_id, e.provider_id, p.display_name AS owner,
           e.category, e.description, e.region || ', ' || e.area AS location,
           CASE WHEN e.active=1 THEN 'Активна' ELSE 'Неактивна' END AS status
      FROM provider_equipment e LEFT JOIN provider_profiles p ON p.telegram_id=e.provider_id
    UNION ALL
    SELECT 'legacy:' || telegram_id, telegram_id, name, equipment, '', location, 'Быстрая регистрация'
      FROM providers
)
ORDER BY provider_id, equipment_id
"""


@asynccontextmanager
async def read_db():
    uri = Path(storage.DB_PATH).resolve().as_uri() + "?mode=ro"
    async with aiosqlite.connect(uri, uri=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA query_only=ON")
        await db.execute("BEGIN")
        yield db


async def rows(db, sql, parameters=()):
    async with db.execute(sql, parameters) as cursor:
        return await cursor.fetchall()


async def user_totals():
    async with read_db() as db:
        return (await rows(db, USERS + """
            SELECT COUNT(*) AS total, COUNT(c.id) AS customers, COUNT(p.id) AS providers,
                   COUNT(CASE WHEN c.id IS NOT NULL AND p.id IS NOT NULL THEN 1 END) AS both
            FROM users u LEFT JOIN customers c ON c.id=u.id LEFT JOIN performers p ON p.id=u.id
        """))[0]


def registration_periods(at=None):
    zone = ZoneInfo("Europe/Moscow")
    current = (at or datetime.now(zone)).astimezone(zone)
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return (day, day - timedelta(days=day.weekday()), day.replace(day=1)), current


async def registration_stats(at=None):
    boundaries, current = registration_periods(at)
    async with read_db() as db:
        result = []
        for start in boundaries:
            result.append((await rows(db, USERS + """
                SELECT (SELECT COUNT(*) FROM registrations WHERE julianday(first_seen) BETWEEN julianday(?) AND julianday(?)) AS providers,
                       (SELECT COUNT(*) FROM customers WHERE julianday(first_seen) BETWEEN julianday(?) AND julianday(?)) AS customers
            """, (start.isoformat(), current.isoformat()) * 2))[0])
        return result


async def equipment_page(offset):
    async with read_db() as db:
        return await rows(db, EQUIPMENT + " LIMIT 9 OFFSET ?", (offset,))


async def lead_and_offer_counts():
    async with read_db() as db:
        leads = await rows(db, "SELECT status, COUNT(*) AS n FROM leads GROUP BY status")
        offers = await rows(db, """
            SELECT l.status, COUNT(*) AS n FROM offers o LEFT JOIN leads l ON l.id=o.lead_id GROUP BY l.status
        """)
        deliveries = await rows(db, "SELECT delivery_status AS status, COUNT(*) AS n FROM offers GROUP BY delivery_status")
    return leads, offers, deliveries


def classify(records):
    result = {"Активные": 0, "Закрытые": 0, "Архивные": 0, "Прочие": 0}
    for row in records:
        status = row["status"]
        if status in {"new", "publishing", "published", "active", "open"}:
            label = "Активные"
        elif status in {"rejected", "closed", "completed", "cancelled"}:
            label = "Закрытые"
        elif status in {"archived", "archive"}:
            label = "Архивные"
        else:
            label = "Прочие"
        result[label] += row["n"]
    return result


def append_text_safe(sheet, values):
    output = []
    for value in values:
        if isinstance(value, str):
            cell = WriteOnlyCell(sheet, value=ILLEGAL_CHARACTERS_RE.sub("", value))
            cell.data_type = "s"
        else:
            cell = WriteOnlyCell(sheet, value=value)
        output.append(cell)
    sheet.append(output)


async def export_xlsx():
    async with read_db() as db:
        users = await rows(db, USERS + """
            SELECT u.id, COALESCE(profile.display_name, legacy.name,
                   (SELECT customer_name FROM leads WHERE customer_id=u.id AND customer_name IS NOT NULL ORDER BY id DESC LIMIT 1),
                   (SELECT provider_name FROM offers WHERE provider_id=u.id ORDER BY id DESC LIMIT 1), '') AS display_name,
                   c.first_seen AS customer_since, r.first_seen AS registered_at,
                   c.id IS NOT NULL AS customer, p.id IS NOT NULL AS performer
            FROM users u LEFT JOIN customers c ON c.id=u.id LEFT JOIN performers p ON p.id=u.id
            LEFT JOIN registrations r ON r.id=u.id LEFT JOIN provider_profiles profile ON profile.telegram_id=u.id
            LEFT JOIN providers legacy ON legacy.telegram_id=u.id ORDER BY u.id
        """)
        equipment = await rows(db, EQUIPMENT)
    workbook = Workbook(write_only=True)
    people = workbook.create_sheet("Пользователи")
    people.append(("Telegram ID", "Имя или компания", "Заказчик", "Исполнитель", "Первая заявка", "Регистрация исполнителя"))
    for user in users:
        append_text_safe(people, (str(user["id"]), user["display_name"], "Да" if user["customer"] else "Нет",
                                  "Да" if user["performer"] else "Нет", user["customer_since"], user["registered_at"]))
    machines = workbook.create_sheet("Техника")
    machines.append(("ID записи", "Telegram ID исполнителя", "Исполнитель", "Категория", "Описание", "Местоположение", "Статус"))
    for item in equipment:
        append_text_safe(machines, (item["equipment_id"], str(item["provider_id"]), item["owner"],
                                    item["category"], item["description"], item["location"], item["status"]))
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()
