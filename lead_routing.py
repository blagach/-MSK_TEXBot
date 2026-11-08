"""Structured lead routing fields; additive and backward compatible."""
import aiosqlite
import provider_registry

async def init_schema(db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS lead_routing (
            lead_id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            region TEXT NOT NULL,
            area TEXT NOT NULL
        )""")
        await db.commit()

def validate(category, region, area):
    if category not in provider_registry.CATEGORIES:
        raise ValueError("Выберите категорию техники из списка")
    if region not in provider_registry.REGIONS:
        raise ValueError("Выберите Москву или Московскую область")
    if not isinstance(area, str) or not 1 <= len(area.strip()) <= 120:
        raise ValueError("Укажите район или населённый пункт")
    return category, region, area.strip()

async def save(lead_id, category, region, area, db_path="bot.db"):
    category, region, area = validate(category, region, area)
    if not isinstance(lead_id, int) or lead_id <= 0:
        raise ValueError("Некорректный номер заявки")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""INSERT INTO lead_routing(lead_id,category,region,area)
            VALUES(?,?,?,?)
            ON CONFLICT(lead_id) DO UPDATE SET
            category=excluded.category,region=excluded.region,area=excluded.area""",
            (lead_id,category,region,area))
        await db.commit()

async def get(lead_id, db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT category,region,area FROM lead_routing WHERE lead_id=?", (lead_id,)) as cursor:
            row=await cursor.fetchone()
            return dict(zip(("category","region","area"),row)) if row else None
