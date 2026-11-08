"""Payment preferences only; this module never transfers money."""
import aiosqlite

CASH = "Наличные"
VAT = "Безналичный расчёт с НДС"
NO_VAT = "Безналичный расчёт без НДС"
ANY = "Любая форма оплаты"
METHODS = {CASH, VAT, NO_VAT}

async def init_schema(db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS lead_payment_preferences (lead_id INTEGER PRIMARY KEY, method TEXT NOT NULL)")
        await db.execute("CREATE TABLE IF NOT EXISTS provider_payment_preferences (provider_id INTEGER NOT NULL, method TEXT NOT NULL, PRIMARY KEY(provider_id, method))")
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

async def eligible_providers(lead_id, provider_ids, db_path="bot.db"):
    if not provider_ids:
        return []
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT method FROM lead_payment_preferences WHERE lead_id=?", (lead_id,)) as cur:
            row = await cur.fetchone()
        if not row or row[0] not in METHODS | {ANY}:
            return []
        requested = row[0]
        placeholders = ",".join("?" for _ in provider_ids)
        async with db.execute(f"SELECT DISTINCT provider_id FROM provider_payment_preferences WHERE provider_id IN ({placeholders}) AND (? = ? OR method = ?)", (*provider_ids, requested, ANY, requested)) as cur:
            eligible = {item[0] for item in await cur.fetchall()}
        return [provider_id for provider_id in provider_ids if provider_id in eligible]
