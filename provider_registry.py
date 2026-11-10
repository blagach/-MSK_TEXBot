"""Provider profiles and equipment inventory for the Moscow-region pilot.

Additive schema: existing providers/leads/offers tables are never altered.
The Telegram UI and notification dispatcher will call this module.
"""
import re
from datetime import datetime, timezone

import aiosqlite

import equipment_catalog

DEFAULT_DB_PATH = "bot.db"
PHONE_RE = re.compile(r"(?:\+7|8)[0-9]{10}\Z")
CATEGORIES = equipment_catalog.TYPES
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
    if not isinstance(phone, str) or not PHONE_RE.fullmatch(phone.strip()):
        raise ValueError("Номер телефона должен быть в формате +7XXXXXXXXXX или 8XXXXXXXXXX")
    number = phone.strip()
    return "+7" + number[-10:]


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


async def add_equipment(telegram_id, category, description, region, area,
                        db_path=DEFAULT_DB_PATH):
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
        await db.commit()
        if cursor.rowcount != 1:
            raise ValueError("Сначала зарегистрируйтесь; заблокированный профиль недоступен")
        return cursor.lastrowid


async def update_equipment(telegram_id, equipment_id, *, category, description,
                           region, area, active=True, db_path=DEFAULT_DB_PATH):
    if category not in CATEGORIES or region not in SERVICE_REGIONS:
        raise ValueError("Некорректная категория или регион")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 200:
        raise ValueError("Некорректное описание")
    if not isinstance(area, str) or not 1 <= len(area.strip()) <= 120:
        raise ValueError("Некорректный район")
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("""UPDATE provider_equipment SET
            category=?, description=?, region=?, area=?, active=?
            WHERE id=? AND provider_id=? AND EXISTS (
                SELECT 1 FROM provider_profiles WHERE telegram_id=? AND blocked=0
            )""", (category, description.strip(), region, area.strip(), int(bool(active)),
                   equipment_id, telegram_id, telegram_id))
        await db.commit()
        return cursor.rowcount == 1


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
        async with db.execute("""SELECT id, category, description, region, area, active
            FROM provider_equipment WHERE provider_id=? ORDER BY id""",
            (telegram_id,)) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

def normalize_area(value):
    return " ".join((value or "").casefold().replace("ё", "е").split())


def equipment_match(category, region, area):
    return f"""(e.category={category} OR (e.category IN ('каток','дорожный каток')
                       AND {category} IN ('каток','дорожный каток')))
              AND (e.region={region} OR e.region='Москва и Московская область')
              AND (area_key(e.area)=area_key({area}) OR area_key(e.area)='весь регион')"""


async def register_area_key(db):
    await db.create_function("area_key", 1, normalize_area, deterministic=True)


async def matching_providers(category, region, area, db_path=DEFAULT_DB_PATH, *, notifications=True):
    """Exact area match, or a provider covering the entire selected region.

    More flexible district matching must use structured locations, not substrings.
    """
    if category not in CATEGORIES or region not in REGIONS:
        return []
    async with aiosqlite.connect(db_path) as db:
        await register_area_key(db)
        predicate = equipment_match("target.category", "target.region", "target.area")
        async with db.execute(f"""WITH target(category, region, area) AS (VALUES (?, ?, ?))
            SELECT DISTINCT p.telegram_id FROM provider_profiles p
            JOIN provider_equipment e ON e.provider_id=p.telegram_id CROSS JOIN target
            WHERE p.blocked=0 AND (p.notifications_enabled=1 OR ?=0)
              AND e.active=1 AND {predicate} ORDER BY p.telegram_id""",
            (category, region, area, notifications),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]
