"""Provider profiles and equipment inventory for the Moscow-region pilot.

Additive schema: existing providers/leads/offers tables are never altered.
The Telegram UI and notification dispatcher will call this module.
"""
import re
from datetime import datetime, timezone

import aiosqlite

import payment_preferences

DEFAULT_DB_PATH = "bot.db"
SERVICE_DIRECTIONS = (
    "Вывоз грунта", "Вывоз снега", "Вывоз строительных отходов",
    "Перевозка сыпучих материалов", "Доставка песка, щебня и других материалов",
    "Приём грунта и отходов на площадках",
)
MACHINE_FIELDS = ("brand", "model", "specifications", "attachments", "notes")
PHONE_RE = re.compile(r"(?:\+7|8)[0-9]{10}\Z")
CATEGORIES = frozenset({
    "мини-экскаватор", "мини-погрузчик", "экскаватор-погрузчик",
    "экскаватор", "манипулятор", "автокран", "каток",
    "самосвал", "трал", "другая техника",
})
REGIONS = frozenset({"Москва", "Московская область"})
BOTH_REGIONS = "Москва и Московская область"
SERVICE_REGIONS = REGIONS | {BOTH_REGIONS}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_name(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
        raise ValueError("Укажите имя или название компании (до 120 символов)")
    return name.strip()


def _validate_phone(phone):
    if not isinstance(phone, str) or len(phone) > 32:
        raise ValueError("Номер телефона должен начинаться с +7 или 8 и содержать 11 цифр")
    compact = phone.translate(str.maketrans("", "", " ()-\u00a0\u202f"))
    if compact.startswith("7"):
        compact = "+" + compact  # Telegram contact cards can omit the plus.
    if not PHONE_RE.fullmatch(compact):
        raise ValueError("Номер телефона должен начинаться с +7 или 8 и содержать 11 цифр")
    return "+7" + compact[-10:]



async def init_schema(db_path=DEFAULT_DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS provider_profiles (
            telegram_id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            notifications_enabled INTEGER NOT NULL DEFAULT 1 CHECK(notifications_enabled IN (0,1)),
            blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
            consent_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS provider_equipment (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_id INTEGER NOT NULL REFERENCES provider_profiles(telegram_id),
            category TEXT NOT NULL,
            description TEXT NOT NULL,
            region TEXT NOT NULL,
            area TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL
        )""")
        await db.execute("""CREATE INDEX IF NOT EXISTS idx_provider_equipment_matching
            ON provider_equipment(category, region, active)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS provider_equipment_details (
            equipment_id INTEGER PRIMARY KEY REFERENCES provider_equipment(id),
            brand TEXT NOT NULL, model TEXT NOT NULL, specifications TEXT NOT NULL,
            attachments TEXT NOT NULL, notes TEXT NOT NULL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS provider_services (
            provider_id INTEGER NOT NULL REFERENCES provider_profiles(telegram_id),
            direction TEXT NOT NULL, selected INTEGER NOT NULL CHECK(selected IN (0,1)),
            PRIMARY KEY(provider_id, direction)
        )""")
        await db.commit()


async def register_provider(telegram_id, name, phone, consent, db_path=DEFAULT_DB_PATH):
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        raise ValueError("Некорректный Telegram ID")
    if consent is not True:
        raise ValueError("Для регистрации необходимо согласие на обработку контактных данных")
    name, phone = _validate_name(name), _validate_phone(phone)
    now = _now()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""INSERT INTO provider_profiles
            (telegram_id, display_name, phone, consent_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                display_name=excluded.display_name,
                phone=excluded.phone,
                updated_at=excluded.updated_at""",
            (telegram_id, name, phone, now, now))
        await db.commit()


async def register_new_provider(telegram_id, name, phone, methods, db_path=DEFAULT_DB_PATH):
    """Save a completed signup once, preserving existing profiles and preferences."""
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        raise ValueError("Некорректный Telegram ID")
    name, phone = _validate_name(name), _validate_phone(phone)
    if not methods or not methods <= payment_preferences.METHODS:
        raise ValueError("Выберите принимаемые формы оплаты")
    now = _now()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""INSERT INTO provider_profiles
            (telegram_id, display_name, phone, consent_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO NOTHING""",
            (telegram_id, name, phone, now, now))
        if cursor.rowcount != 1:
            return False
        await db.executemany("""INSERT INTO provider_payment_preferences (provider_id, method)
            VALUES (?, ?) ON CONFLICT(provider_id, method) DO NOTHING""",
            [(telegram_id, method) for method in sorted(methods)])
        await db.commit()
        return True


async def add_equipment(telegram_id, category, description, region, area,
                        db_path=DEFAULT_DB_PATH, *, fields=None):
    if category not in CATEGORIES or region not in SERVICE_REGIONS:
        raise ValueError("Выберите категорию техники и регион из списка")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 200:
        raise ValueError("Описание техники: от 1 до 200 символов")
    if not isinstance(area, str) or not 1 <= len(area.strip()) <= 120:
        raise ValueError("Укажите район или «весь регион»")
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""INSERT INTO provider_equipment
            (provider_id, category, description, region, area, created_at)
            SELECT telegram_id, ?, ?, ?, ?, ? FROM provider_profiles
            WHERE telegram_id=? AND blocked=0""",
            (category, description.strip(), region, area.strip(), _now(), telegram_id))
        if cursor.rowcount != 1:
            raise ValueError("Сначала зарегистрируйтесь; заблокированный профиль недоступен")
        if fields is not None:
            await write_machine_details(db, cursor.lastrowid, fields)
        await db.commit()
        return cursor.lastrowid


async def update_equipment(telegram_id, equipment_id, *, category, description,
                           region, area, active=True, db_path=DEFAULT_DB_PATH, fields=None):
    if category not in CATEGORIES or region not in SERVICE_REGIONS:
        raise ValueError("Некорректная категория или регион")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 200:
        raise ValueError("Некорректное описание")
    if not isinstance(area, str) or not 1 <= len(area.strip()) <= 120:
        raise ValueError("Некорректный район")
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""UPDATE provider_equipment SET
            category=?, description=?, region=?, area=?, active=COALESCE(?, active)
            WHERE id=? AND provider_id=? AND EXISTS (
                SELECT 1 FROM provider_profiles WHERE telegram_id=? AND blocked=0
            )""", (category, description.strip(), region, area.strip(), None if active is None else int(bool(active)),
                   equipment_id, telegram_id, telegram_id))
        if cursor.rowcount and fields is not None:
            await write_machine_details(db, equipment_id, fields)
        await db.commit()
        return cursor.rowcount == 1


def validate_equipment_field(field, value):
    if field in ("category", "region"):
        options = CATEGORIES if field == "category" else SERVICE_REGIONS
        if value not in options:
            raise ValueError("Выберите значение из списка.")
    elif field in {*MACHINE_FIELDS, "area"}:
        limit = 120 if field == "area" else 200
        required = field in {"area", "brand", "model"}
        if not isinstance(value, str) or len(value) > limit or (required and not value):
            raise ValueError(f"Введите значение длиной до {limit} символов.")
    else:
        raise ValueError("Неизвестное поле техники.")
    return value


async def edit_equipment_field(telegram_id, equipment_id, field, value, db_path=DEFAULT_DB_PATH):
    value = validate_equipment_field(field, value)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        owned = await (await db.execute(
            """SELECT e.description FROM provider_equipment e
               JOIN provider_profiles p ON p.telegram_id=e.provider_id
               WHERE e.id=? AND e.provider_id=? AND p.blocked=0""",
            (equipment_id, telegram_id),
        )).fetchone()
        if not owned:
            return False
        if field in MACHINE_FIELDS:
            await db.execute(
                "INSERT OR IGNORE INTO provider_equipment_details VALUES (?,'','','','',?)",
                (equipment_id, owned[0]),
            )
            await db.execute(f"UPDATE provider_equipment_details SET {field}=? WHERE equipment_id=?",
                             (value, equipment_id))
        else:
            await db.execute(f"UPDATE provider_equipment SET {field}=? WHERE id=?", (value, equipment_id))
        await db.commit()
        return True


async def write_machine_details(db, equipment_id, fields):
    await db.execute("""INSERT INTO provider_equipment_details VALUES (?,?,?,?,?,?)
        ON CONFLICT(equipment_id) DO UPDATE SET brand=excluded.brand, model=excluded.model,
        specifications=excluded.specifications, attachments=excluded.attachments, notes=excluded.notes""",
        (equipment_id, *(fields[key] for key in MACHINE_FIELDS)))


async def get_profile(telegram_id, db_path=DEFAULT_DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute(
            "SELECT * FROM provider_profiles WHERE telegram_id=?", (telegram_id,)
        )).fetchone()


async def edit_profile(telegram_id, field, value, db_path=DEFAULT_DB_PATH):
    columns = {"name": "display_name", "phone": "phone"}
    if field not in {*columns, "payment"}:
        raise ValueError("Неизвестное поле профиля")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        if field == "payment":
            cursor = await db.execute(
                "UPDATE provider_profiles SET updated_at=? WHERE telegram_id=? AND blocked=0",
                (_now(), telegram_id),
            )
            if cursor.rowcount:
                await payment_preferences.write_provider_revision(db, telegram_id, value)
        else:
            column = columns[field]
            value = _validate_name(value) if field == "name" else _validate_phone(value)
            cursor = await db.execute(
                f"UPDATE provider_profiles SET {column}=?, updated_at=? WHERE telegram_id=? AND blocked=0",
                (value, _now(), telegram_id),
            )
        await db.commit()
        return cursor.rowcount == 1


async def toggle_notifications(telegram_id, expected, db_path=DEFAULT_DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """UPDATE provider_profiles SET notifications_enabled=?, updated_at=?
               WHERE telegram_id=? AND blocked=0 AND notifications_enabled=?""",
            (int(not expected), _now(), telegram_id, int(expected)),
        )
        await db.commit()


async def change_equipment_status(telegram_id, equipment_id, active, db_path=DEFAULT_DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """UPDATE provider_equipment SET active=? WHERE id=? AND provider_id=?
               AND EXISTS (SELECT 1 FROM provider_profiles WHERE telegram_id=? AND blocked=0)""",
            (int(active), equipment_id, telegram_id, telegram_id),
        )
        await db.commit()
        return cursor.rowcount == 1


async def provider_services(telegram_id, db_path=DEFAULT_DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT direction FROM provider_services WHERE provider_id=? AND selected=1",
            (telegram_id,),
        )).fetchall()
        return [row[0] for row in rows]


async def save_services(telegram_id, selected, db_path=DEFAULT_DB_PATH):
    if not set(selected) <= set(SERVICE_DIRECTIONS):
        raise ValueError("Неизвестное направление")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        exists = await (await db.execute(
            "SELECT 1 FROM provider_profiles WHERE telegram_id=? AND blocked=0", (telegram_id,)
        )).fetchone()
        if not exists:
            return False
        await db.executemany(
            """INSERT INTO provider_services VALUES (?,?,?) ON CONFLICT(provider_id,direction)
               DO UPDATE SET selected=excluded.selected""",
            [(telegram_id, direction, int(direction in selected)) for direction in SERVICE_DIRECTIONS],
        )
        await db.commit()
        return True


async def set_notifications(telegram_id, enabled, db_path=DEFAULT_DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""UPDATE provider_profiles SET
            notifications_enabled=?, updated_at=?
            WHERE telegram_id=? AND blocked=0""",
            (int(bool(enabled)), _now(), telegram_id))
        await db.commit()
        return cursor.rowcount == 1


async def set_blocked(telegram_id, blocked, *, admin_id, admin_ids,
                      db_path=DEFAULT_DB_PATH):
    if admin_id not in admin_ids:
        raise PermissionError("Только администратор может блокировать исполнителей")
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""UPDATE provider_profiles
            SET blocked=?, updated_at=? WHERE telegram_id=?""",
            (int(bool(blocked)), _now(), telegram_id))
        await db.commit()
        return cursor.rowcount == 1


async def list_equipment(telegram_id, db_path=DEFAULT_DB_PATH):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""SELECT e.id, e.category, e.description, e.region, e.area, e.active,
                   d.brand, d.model, d.specifications, d.attachments, d.notes
            FROM provider_equipment e LEFT JOIN provider_equipment_details d ON d.equipment_id=e.id
            WHERE e.provider_id=? ORDER BY e.id""",
            (telegram_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def matching_providers(category, region, area, db_path=DEFAULT_DB_PATH):
    """Exact area match, or a provider covering the entire selected region.

    More flexible district matching must use structured locations, not substrings.
    """
    if category not in CATEGORIES or region not in REGIONS:
        return []
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("""SELECT DISTINCT p.telegram_id
            FROM provider_profiles p
            JOIN provider_equipment e ON e.provider_id=p.telegram_id
            WHERE p.blocked=0 AND p.notifications_enabled=1
              AND e.active=1 AND e.category=? AND (e.region=? OR e.region=?)
              AND (e.area=? OR e.area='весь регион')
            ORDER BY p.telegram_id""", (category, region, BOTH_REGIONS, area)) as cursor:
            return [row[0] for row in await cursor.fetchall()]
