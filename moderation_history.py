"""Additive approval history and atomic publication claims (no Telegram calls)."""
import aiosqlite

async def init_schema(db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS customer_approvals (
            customer_id INTEGER PRIMARY KEY, approved_lead_id INTEGER NOT NULL,
            approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS publication_claims (
            lead_id INTEGER PRIMARY KEY, claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            outcome TEXT NOT NULL DEFAULT 'pending')""")
        await db.commit()

async def approved_before(customer_id, db_path="bot.db"):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT 1 FROM customer_approvals WHERE customer_id=?", (customer_id,)) as cur:
            return await cur.fetchone() is not None

async def record_approval(customer_id, lead_id, db_path="bot.db"):
    if not isinstance(customer_id,int) or customer_id<=0 or not isinstance(lead_id,int) or lead_id<=0:
        raise ValueError("Invalid IDs")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""INSERT INTO customer_approvals(customer_id,approved_lead_id)
            VALUES(?,?) ON CONFLICT(customer_id) DO NOTHING""",(customer_id,lead_id))
        await db.commit()

async def claim_publication(lead_id, db_path="bot.db"):
    """Only one attempt may claim a lead. Unknown Telegram outcomes require manual reconciliation."""
    if not isinstance(lead_id,int) or lead_id<=0:
        raise ValueError("Invalid lead ID")
    async with aiosqlite.connect(db_path) as db:
        cur=await db.execute("INSERT OR IGNORE INTO publication_claims(lead_id) VALUES(?)",(lead_id,))
        await db.commit()
        return cur.rowcount==1

async def mark_outcome(lead_id, outcome, db_path="bot.db"):
    if outcome not in ("confirmed","unknown","rejected"):
        raise ValueError("Invalid outcome")
    async with aiosqlite.connect(db_path) as db:
        cur=await db.execute("UPDATE publication_claims SET outcome=? WHERE lead_id=?",(outcome,lead_id))
        await db.commit()
        return cur.rowcount==1
