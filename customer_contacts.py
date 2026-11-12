"""Free, idempotent contact access. No payments or Telegram side effects."""
import aiosqlite
import user_access

async def init_schema(path):
    async with aiosqlite.connect(path) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS customer_contact_access (
            provider_id INTEGER NOT NULL, lead_id INTEGER NOT NULL REFERENCES leads(id),
            phone TEXT NOT NULL, PRIMARY KEY(provider_id,lead_id))''')
        await db.commit()

async def open_contact(path, provider_id, lead_id):
    async with aiosqlite.connect(path) as db:
        await db.execute('BEGIN IMMEDIATE')
        if not await user_access.allowed_in(db,provider_id,'offers'):
            raise PermissionError('Доступ к контактам ограничен. Доступна /support.')
        profile=await (await db.execute('SELECT blocked FROM provider_profiles WHERE telegram_id=?',(provider_id,))).fetchone()
        if not profile or profile[0]:
            raise PermissionError('Сначала зарегистрируйтесь как исполнитель.')
        old=await (await db.execute('SELECT phone FROM customer_contact_access WHERE provider_id=? AND lead_id=?',(provider_id,lead_id))).fetchone()
        if old:
            return old[0]
        lead=await (await db.execute('SELECT customer_id,status,phone FROM leads WHERE id=?',(lead_id,))).fetchone()
        if not lead or lead[1]!='published' or lead[0]==provider_id:
            raise PermissionError('Контакт недоступен: заявка закрыта или не опубликована.')
        await db.execute('INSERT INTO customer_contact_access VALUES (?,?,?)',(provider_id,lead_id,lead[2]))
        await db.commit()
        return lead[2]
