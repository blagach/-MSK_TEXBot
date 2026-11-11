"""Read-only operations and export for the administrator panel."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

import storage
from lead_journey import OFFER_RESULT, attachments_text
from provider_registry import _validate_phone

USERS = """
WITH customers AS (
    SELECT customer_id AS id, MIN(report_utc(created_at)) AS first_seen FROM leads GROUP BY customer_id
), registrations AS (
    SELECT id, MIN(report_utc(registered_at)) AS first_seen FROM (
        SELECT telegram_id AS id, consent_at AS registered_at FROM provider_profiles
        UNION ALL SELECT telegram_id, created_at FROM providers
    ) GROUP BY id
), performers AS (
    SELECT id FROM registrations UNION SELECT provider_id FROM offers
), users AS (
    SELECT id FROM customers UNION SELECT id FROM performers UNION SELECT requester_id FROM support_tickets
    UNION SELECT telegram_id FROM bot_visitors
)
"""
USER_DETAILS = """
            SELECT u.id, COALESCE(NULLIF(TRIM(profile.display_name), ''), NULLIF(TRIM(legacy.name), ''),
                   (SELECT customer_name FROM leads WHERE customer_id=u.id AND TRIM(customer_name)!='' ORDER BY id DESC LIMIT 1),
                   (SELECT provider_name FROM offers WHERE provider_id=u.id AND TRIM(provider_name)!='' ORDER BY id DESC LIMIT 1)) AS display_name,
                   COALESCE(report_phone(profile.phone), report_phone(legacy.phone),
                            (SELECT report_phone(l.phone) FROM leads l WHERE l.customer_id=u.id
                             AND report_phone(l.phone) IS NOT NULL ORDER BY l.id DESC LIMIT 1),
                            (SELECT report_phone(o.phone) FROM offers o WHERE o.provider_id=u.id
                             AND report_phone(o.phone) IS NOT NULL ORDER BY o.id DESC LIMIT 1)) AS phone,
                   COALESCE((SELECT report_username(d.customer_username) FROM lead_work_details d JOIN leads l
                             ON l.id=d.lead_id WHERE l.customer_id=u.id AND report_username(d.customer_username) IS NOT NULL
                             ORDER BY l.id DESC LIMIT 1),
                            (SELECT report_username(o.username) FROM offers o WHERE o.provider_id=u.id
                             AND report_username(o.username) IS NOT NULL ORDER BY o.id DESC LIMIT 1)) AS username,
                   c.first_seen AS customer_since, r.first_seen AS registered_at,
                   c.id IS NOT NULL AS customer, p.id IS NOT NULL AS performer,
                   COALESCE(profile.blocked,0) AS legacy_blocked
            FROM users u LEFT JOIN customers c ON c.id=u.id LEFT JOIN performers p ON p.id=u.id
            LEFT JOIN registrations r ON r.id=u.id LEFT JOIN provider_profiles profile ON profile.telegram_id=u.id
            LEFT JOIN providers legacy ON legacy.telegram_id=u.id
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
        await db.create_function("report_utc", 1, utc_report_timestamp, deterministic=True)
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


def utc_report_timestamp(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        return None


async def registration_stats(at=None):
    boundaries, current = registration_periods(at)
    async with read_db() as db:
        # SQLite julianday rounds sub-millisecond values at period boundaries.
        result = []
        for start in boundaries:
            result.append((await rows(db, """
                WITH period(start_at, end_at) AS (VALUES (report_utc(?), report_utc(?))),
                created AS (
                    SELECT customer_id, report_utc(created_at) AS at FROM leads
                ),
                first_requests AS (
                    SELECT MIN(at) AS at FROM created GROUP BY customer_id
                ),
                registrations AS (
                    SELECT MIN(report_utc(registered_at)) AS at FROM (
                        SELECT telegram_id, consent_at AS registered_at FROM provider_profiles
                        UNION ALL SELECT telegram_id, created_at FROM providers
                    ) GROUP BY telegram_id
                )
                SELECT (SELECT COUNT(*) FROM registrations WHERE at BETWEEN start_at AND end_at) AS providers,
                       (SELECT COUNT(*) FROM first_requests WHERE at BETWEEN start_at AND end_at) AS customers,
                       (SELECT COUNT(*) FROM created WHERE at BETWEEN start_at AND end_at) AS created,
                       (SELECT COUNT(DISTINCT lead_id) FROM lead_lifecycle_events WHERE action='published'
                            AND report_utc(created_at) BETWEEN start_at AND end_at) AS published,
                       (SELECT COUNT(*) FROM offers WHERE report_utc(created_at) BETWEEN start_at AND end_at) AS offers
                FROM period
            """, (start.isoformat(), current.isoformat())))[0])
        return result


async def equipment_totals():
    async with read_db() as db:
        return (await rows(db, USERS + """
            SELECT (SELECT count(*) FROM registrations) AS providers,
                   (SELECT count(*) FROM provider_equipment) AS units
        """))[0]


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
        results = await rows(db, f"""
            SELECT {OFFER_RESULT} AS status, COUNT(*) AS n
            FROM offers o LEFT JOIN leads l ON l.id=o.lead_id
            LEFT JOIN lead_lifecycle_events e ON e.lead_id=o.lead_id AND e.action='selected'
            GROUP BY 1 ORDER BY 1
        """)
    return leads, offers, deliveries, results


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
        await db.create_function("report_phone", 1, normalized_phone, deterministic=True)
        await db.create_function("report_username", 1, profile_name, deterministic=True)
        users = await rows(db, USERS + USER_DETAILS + " ORDER BY u.id")
        equipment = await rows(db, EQUIPMENT)
        leads = await rows(db, """
            SELECT l.*, r.region, r.area, d.equipment_name, d.map_link, d.schedule, p.method,
                   a.selected_json AS attachments_json, a.custom_text AS attachments_custom
            FROM leads l LEFT JOIN lead_routing r ON r.lead_id=l.id
            LEFT JOIN lead_work_details d ON d.lead_id=l.id
            LEFT JOIN lead_payment_preferences p ON p.lead_id=l.id
            LEFT JOIN lead_attachments a ON a.lead_id=l.id ORDER BY l.id
        """)
        offers = await rows(db, f"""
            SELECT o.*, n.note, {OFFER_RESULT} AS result
            FROM offers o LEFT JOIN leads l ON l.id=o.lead_id
            LEFT JOIN offer_notes n ON n.offer_id=o.id
            LEFT JOIN lead_lifecycle_events e ON e.lead_id=o.lead_id AND e.action='selected' ORDER BY o.id
        """)
        tickets = await rows(db, """
            SELECT t.*, (SELECT count(*) FROM support_messages m WHERE m.ticket_id=t.id) AS messages,
                   (SELECT m.text FROM support_messages m WHERE m.ticket_id=t.id ORDER BY m.id LIMIT 1) AS opening
            FROM support_tickets t ORDER BY t.id
        """)
        events = await rows(db, """
            SELECT 'Заявка' AS entity, lead_id AS record_id, actor_id, action, reason, offer_id, created_at
            FROM lead_lifecycle_events
            UNION ALL
            SELECT 'Обращение', ticket_id, actor_id, status, NULL, NULL, created_at FROM support_status_events
            ORDER BY created_at, entity, record_id
        """)
    workbook = Workbook(write_only=True)
    people = workbook.create_sheet("Пользователи")
    people.append(("Telegram ID", "Имя или компания", "Заказчик", "Исполнитель", "Первая заявка", "Регистрация исполнителя", "Телефон", "Username", "Профиль"))
    for user in users:
        append_text_safe(people, (str(user["id"]), user["display_name"], "Да" if user["customer"] else "Нет",
                                  "Да" if user["performer"] else "Нет", moscow_time(user["customer_since"]), moscow_time(user["registered_at"]),
                                  normalized_phone(user["phone"]), profile_name(user["username"]),
                                  profile_url(user["username"])))
    machines = workbook.create_sheet("Техника")
    machines.append(("ID записи", "Telegram ID исполнителя", "Исполнитель", "Категория", "Описание", "Местоположение", "Статус"))
    for item in equipment:
        append_text_safe(machines, (item["equipment_id"], str(item["provider_id"]), item["owner"],
                                    item["category"], item["description"], item["location"], item["status"]))
    orders = workbook.create_sheet("Заявки")
    orders.append(("№", "Telegram ID заказчика", "Имя", "Техника", "Уточнение техники", "Регион", "Район",
                   "Адрес", "Карта", "Дата и время работ", "График", "Описание", "Оплата", "Телефон", "Статус", "Создано, МСК", "Допоборудование"))
    for lead in leads:
        append_text_safe(orders, (lead["id"], str(lead["customer_id"]), lead["customer_name"], lead["equipment"],
                                  lead["equipment_name"], lead["region"], lead["area"], lead["place"], lead["map_link"],
                                  lead["work_date"], lead["schedule"], lead["details"], lead["method"],
                                  normalized_phone(lead["phone"]), lead["status"], moscow_time(lead["created_at"]), attachments_text(dict(lead))))
    responses = workbook.create_sheet("Отклики")
    responses.append(("№", "№ заявки", "Telegram ID исполнителя", "Исполнитель", "Username", "Профиль",
                      "Телефон", "Стоимость", "Условия", "Результат", "Доставка", "Создано, МСК", "Комментарий"))
    for offer in offers:
        append_text_safe(responses, (offer["id"], offer["lead_id"], str(offer["provider_id"]), offer["provider_name"],
                                     profile_name(offer["username"]), profile_url(offer["username"]),
                                     normalized_phone(offer["phone"]), offer["price"], offer["comment"], offer["result"],
                                     offer["delivery_status"], moscow_time(offer["created_at"]), offer["note"]))
    requests = workbook.create_sheet("Обращения")
    requests.append(("№", "Telegram ID автора", "Тип", "Статус", "Первое сообщение", "Всего сообщений", "Создано, МСК"))
    for ticket in tickets:
        append_text_safe(requests, (ticket["id"], str(ticket["requester_id"]), ticket["kind"], ticket["status"],
                                    ticket["opening"], ticket["messages"], moscow_time(ticket["created_at"])))
    history = workbook.create_sheet("История статусов и закрытия")
    history.append(("Тип записи", "Номер", "Telegram ID автора", "Событие", "Причина", "№ отклика", "Дата, МСК"))
    for event in events:
        append_text_safe(history, (event["entity"], event["record_id"],
                                   str(event["actor_id"]) if event["actor_id"] is not None else None,
                                   event["action"], event["reason"], event["offer_id"], moscow_time(event["created_at"])))
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def moscow_time(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZoneInfo("Europe/Moscow")).isoformat(timespec="seconds")


def normalized_phone(value):
    try:
        return _validate_phone(value)
    except ValueError:
        return None


def profile_name(username):
    if not username:
        return None
    handle = username.lstrip("@")
    return handle if handle.isascii() and handle.replace("_", "").isalnum() else None


def profile_url(username):
    handle = profile_name(username)
    return f"https://t.me/{handle}" if handle else None


def folded(value):
    return str(value or "").strip().casefold().replace("ё", "е")


async def user_page(query="", kind="all", after=0, limit=10):
    if kind not in {"all", "customers", "providers", "restricted"}:
        raise ValueError("Unknown user filter")
    query = query.strip()
    if len(query) > 100:
        raise ValueError("Search is too long")
    normalized = normalized_phone(query.translate(str.maketrans("", "", " ()-\u00a0\u202f"))) or query
    async with read_db() as db:
        await db.create_function("report_phone", 1, normalized_phone, deterministic=True)
        await db.create_function("report_username", 1, profile_name, deterministic=True)
        await db.create_function("report_fold", 1, folded, deterministic=True)
        return await rows(db, USERS + ", details AS (" + USER_DETAILS + ")" + """
            SELECT * FROM details WHERE id>? AND
                (?='all' OR (?='customers' AND customer=1) OR (?='providers' AND performer=1)
                 OR (?='restricted' AND (legacy_blocked=1 OR COALESCE((SELECT scopes_json FROM user_access_events a
                    WHERE a.user_id=details.id ORDER BY a.id DESC LIMIT 1),'[]')!='[]'))) AND
                (?='' OR CAST(id AS TEXT)=? OR instr(report_fold(display_name),report_fold(?))>0
                      OR instr(report_fold(username),report_fold(?))>0 OR phone=?)
            ORDER BY id LIMIT ?
        """, (after, kind, kind, kind, kind, query, query, query, (query[1:] if query.startswith("@") and len(query)>1 else query), normalized, min(max(limit,1),21)))


async def user_profile(user_id):
    people = await user_page(str(user_id), after=user_id-1, limit=1)
    if not people or people[0]["id"] != user_id:
        return None
    async with read_db() as db:
        counts = (await rows(db, """SELECT
            (SELECT count(*) FROM leads WHERE customer_id=?) AS leads,
            (SELECT count(*) FROM offers WHERE provider_id=?) AS offers,
            (SELECT count(*) FROM provider_equipment WHERE provider_id=?) AS equipment,
            (SELECT count(*) FROM support_tickets WHERE requester_id=?) AS tickets
        """, (user_id,)*4))[0]
    return {**dict(people[0]), **dict(counts)}


async def administrator_leads(admin_id, kind='all', before=2**63-1, limit=10):
    filters = {
        'new': "l.status IN ('new','publishing','published') AND NOT EXISTS(SELECT 1 FROM admin_lead_views v WHERE v.admin_id=? AND v.lead_id=l.id)",
        'active': "l.status IN ('new','publishing','published')",
        'closed': "l.status IN ('closed','completed','rejected','cancelled')",
        'all': '1=1',
    }
    if kind not in filters:
        raise ValueError('Unknown lead filter')
    params=(before,admin_id,limit) if kind=='new' else (before,limit)
    async with read_db() as db:
        return await rows(db,'SELECT l.id,l.equipment,l.status,l.created_at FROM leads l WHERE l.id<? AND '+
                          filters[kind]+' ORDER BY l.id DESC LIMIT ?',params)
