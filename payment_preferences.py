"""Payment preferences only; this module never transfers money."""
import json

import aiosqlite

CASH = "Наличные"
VAT = "Безналичный расчёт с НДС"
NO_VAT = "Безналичный расчёт без НДС"
ANY = "Любая форма оплаты"
PAYMENT_OPTIONS = (CASH, VAT, NO_VAT)
METHODS = set(PAYMENT_OPTIONS)

async def init_schema(db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS lead_payment_preferences (lead_id INTEGER PRIMARY KEY, method TEXT NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS provider_payment_preferences (provider_id INTEGER NOT NULL, method TEXT NOT NULL, PRIMARY KEY(provider_id, method))")
        await db.execute("""CREATE TABLE IF NOT EXISTS provider_payment_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, provider_id INTEGER NOT NULL, methods TEXT NOT NULL
        )""")
        await db.commit()

async def write_lead(db, lead_id, method):
    if method not in METHODS | {ANY}:
        raise ValueError("Некорректная форма оплаты")
    await db.execute(
        "INSERT INTO lead_payment_preferences VALUES (?,?) "
        "ON CONFLICT(lead_id) DO UPDATE SET method=excluded.method", (lead_id, method),
    )


async def save_lead(lead_id, method, db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        await write_lead(db, lead_id, method)
        await db.commit()


async def save_provider(provider_id, methods, db_path="bot.db"):
    methods = set(methods)
    if not methods or not methods <= METHODS:
        raise ValueError("Выберите принимаемые формы оплаты")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM provider_payment_preferences WHERE provider_id=?", (provider_id,))
        await db.executemany("INSERT INTO provider_payment_preferences VALUES (?,?)", [(provider_id, method) for method in methods])
        await db.commit()

async def read_provider(db, provider_id):
    revision = await (await db.execute(
        "SELECT methods FROM provider_payment_revisions WHERE provider_id=? ORDER BY id DESC LIMIT 1",
        (provider_id,),
    )).fetchone()
    if revision:
        return json.loads(revision[0])
    rows = await (await db.execute(
        "SELECT method FROM provider_payment_preferences WHERE provider_id=? ORDER BY method",
        (provider_id,),
    )).fetchall()
    return [row[0] for row in rows]


async def write_provider_revision(db, provider_id, methods):
    if not methods or not set(methods) <= METHODS:
        raise ValueError("Выберите принимаемые формы оплаты")
    await db.execute(
        "INSERT INTO provider_payment_revisions(provider_id,methods) VALUES (?,?)",
        (provider_id, json.dumps(sorted(methods), ensure_ascii=False)),
    )


async def eligible_providers(lead_id, provider_ids, db_path="bot.db"):
    if not provider_ids:
        return []
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT method FROM lead_payment_preferences WHERE lead_id=?", (lead_id,)) as cur:
            row = await cur.fetchone()
        if not row or row[0] not in METHODS | {ANY}:
            return []
        requested = row[0]
        return [provider_id for provider_id in provider_ids
                if (methods := await read_provider(db, provider_id))
                and (requested == ANY or requested in methods)]
