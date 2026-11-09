import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.update(BOT_TOKEN='123456:TEST', ADMIN_IDS='1,2', LEADS_CHANNEL_ID='-100123')
import aiosqlite
import bot
import storage
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(storage, 'DB_PATH', self.temp.name + '/test.db')
        self.db_patch.start()
        await bot.init_db()
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute("""INSERT INTO leads
                (customer_id,equipment,place,work_date,details,phone,created_at)
                VALUES (10,'кран','Москва','завтра','работа','+79991234567','now')""")
            await db.commit()
        self.telegram = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(id=-100123), message_id=99)))
        self.callback = SimpleNamespace(from_user=SimpleNamespace(id=1), data='publish:1',
                                        answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    async def status(self):
        return (await bot.get_lead(1))['status']

    async def test_migration_preserves_existing_data_and_is_repeatable(self):
        await bot.init_db()
        await bot.init_db()
        self.assertEqual((await bot.get_lead(1))['equipment'], 'кран')
        self.assertEqual(await self.status(), 'new')

    async def test_upgrade_from_original_database(self):
        async with aiosqlite.connect(storage.DB_PATH) as db:
            await db.execute('DROP TABLE offers')
            await db.execute('DROP TABLE lead_publications')
            await db.commit()
        await bot.init_db()
        await bot.init_db()
        self.assertEqual((await bot.get_lead(1))['equipment'], 'кран')
        async with aiosqlite.connect(storage.DB_PATH) as db:
            count = await (await db.execute('SELECT count(*) FROM leads')).fetchone()
            offers = await (await db.execute('SELECT count(*) FROM offers')).fetchone()
        self.assertEqual(count[0], 1)
        self.assertEqual(offers[0], 0)

    async def test_concurrent_publication_only_one_send(self):
        await asyncio.gather(*(bot.publish_lead(self.callback, self.telegram) for _ in range(8)))
        self.telegram.send_message.assert_awaited_once()
        self.assertEqual(await self.status(), 'published')
        async with aiosqlite.connect(storage.DB_PATH) as db:
            row = await (await db.execute('SELECT chat_id,message_id FROM lead_publications')).fetchone()
        self.assertEqual(row, (-100123, 99))

    async def test_reject_cannot_override_publication(self):
        await bot.publish_lead(self.callback, self.telegram)
        self.callback.data = 'reject:1'
        await bot.reject_lead(self.callback, self.telegram)
        self.assertEqual(await self.status(), 'published')

    async def test_publish_reject_race_has_one_winner(self):
        results = await asyncio.gather(storage.transition(1, 'new', 'publishing'),
                                       storage.transition(1, 'new', 'rejected'))
        self.assertEqual(sum(results), 1)

    async def test_network_uncertainty_blocks_retry_across_init(self):
        self.telegram.send_message.side_effect = TelegramNetworkError(method=SendMessage(chat_id=1,text='x'), message='timeout')
        await bot.publish_lead(self.callback, self.telegram)
        self.assertEqual(await self.status(), 'publishing')
        await bot.init_db()
        self.telegram.send_message.reset_mock()
        await bot.publish_lead(self.callback, self.telegram)
        self.telegram.send_message.assert_not_awaited()

    async def test_explicit_telegram_rejection_allows_retry(self):
        self.telegram.send_message.side_effect = TelegramBadRequest(method=SendMessage(chat_id=1,text='x'), message='bad request')
        await bot.publish_lead(self.callback, self.telegram)
        self.assertEqual(await self.status(), 'new')

    async def test_expired_callback_does_not_strand_publication(self):
        self.callback.answer.side_effect = TelegramBadRequest(method=SendMessage(chat_id=1,text='x'), message='expired')
        await bot.publish_lead(self.callback, self.telegram)
        self.assertEqual(await self.status(), 'published')

    async def test_non_admin_cannot_publish(self):
        self.callback.from_user.id = 999
        await bot.publish_lead(self.callback, self.telegram)
        self.telegram.send_message.assert_not_awaited()
        self.assertEqual(await self.status(), 'new')

    async def test_malformed_callback(self):
        self.callback.data = 'publish:abc'
        await bot.publish_lead(self.callback, self.telegram)
        self.telegram.send_message.assert_not_awaited()

    async def test_offers_unique_and_closed_lead_rejected(self):
        self.assertIsNone(await storage.save_offer(1, 20, 'n','u','p','c','phone'))
        await storage.transition(1, 'new', 'publishing')
        await storage.finish_publication(1, -1, 1)
        results = await asyncio.gather(*(storage.save_offer(1, 20, 'n','u','p','c','phone') for _ in range(8)))
        self.assertEqual(sum(r is not None for r in results), 1)

    async def test_failed_delivery_offer_is_persisted(self):
        await storage.transition(1, 'new', 'publishing')
        await storage.finish_publication(1, -1, 1)
        message = SimpleNamespace(text='+79991234567', answer=AsyncMock(), from_user=SimpleNamespace(
            id=20, full_name='Исполнитель', username=None))
        state = SimpleNamespace(get_data=AsyncMock(return_value={'lead_id':1,'price':'1000','comment':'условия','phone':'+79991234567'}), clear=AsyncMock())
        self.telegram.send_message.side_effect = TelegramNetworkError(method=SendMessage(chat_id=1,text='x'), message='timeout')
        await bot.send_offer(message, message.from_user, state, self.telegram)
        async with aiosqlite.connect(storage.DB_PATH) as db:
            row = await (await db.execute('SELECT delivery_status,price FROM offers')).fetchone()
        self.assertEqual(row, ('unconfirmed', '1000'))
        state.clear.assert_awaited_once()
        self.assertIn('не подтверждена', message.answer.call_args.args[0])

    async def test_non_text_input_does_not_advance_form(self):
        message = SimpleNamespace(text=None, answer=AsyncMock())
        state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())
        await bot.lead_equipment(message, state)
        state.update_data.assert_not_awaited()
        state.set_state.assert_not_awaited()

    def test_identifiers_and_phone_validation(self):
        for value in ['lead_abc','lead_-1','lead_0','lead_1:2','lead_'+'9'*100, None]:
            self.assertIsNone(storage.parse_id(value, 'lead_'))
        self.assertEqual(storage.parse_id('lead_123', 'lead_'), 123)
        self.assertTrue(storage.valid_text('+79991234567', 32, True))
        self.assertFalse(storage.valid_text('не телефон', 32, True))
        self.assertFalse(storage.valid_text(' '*5, 100))
        self.assertFalse(storage.valid_text('x'*101, 100))


if __name__ == '__main__':
    unittest.main()
