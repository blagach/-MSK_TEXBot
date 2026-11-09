import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
os.environ.update(BOT_TOKEN="123456:TEST", ADMIN_IDS="1,2", LEADS_CHANNEL_ID="-100123")
import bot

class ShadowFlowGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_candidate_does_not_publish(self):
        state = SimpleNamespace(get_data=AsyncMock(return_value={
            "equipment":"каток", "region":"Москва", "area":"СЗАО",
            "place":"улица Свободы", "work_date":"завтра",
            "details":"Работы"}), clear=AsyncMock())
        message = SimpleNamespace(text="+79991234567",
            from_user=SimpleNamespace(id=501, full_name="Тест"),
            answer=AsyncMock())
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot.lead_shadow_review, "evaluate",
                          new_callable=AsyncMock) as shadow, \
             patch.object(bot, "read_lead", new_callable=AsyncMock) as read_lead, \
             patch.object(bot.lead_routing, "save", new_callable=AsyncMock), \
             patch.object(bot.aiosqlite, "connect") as connect:
            shadow.return_value = SimpleNamespace(route="candidate")
            read_lead.return_value = {"id":1,"customer_id":501,
                "customer_name":"Тест","equipment":"каток","place":"улица Свободы",
                "work_date":"завтра","details":"Работы","phone":"+79991234567",
                "status":"new","payment_method":None}
            db = AsyncMock()
            cursor = SimpleNamespace(lastrowid=1)
            db.execute.return_value = cursor
            cm = AsyncMock()
            cm.__aenter__.return_value = db
            connect.return_value = cm
            await bot.lead_phone(message,state,fake_bot)
            self.assertFalse(shadow.await_args.kwargs["enabled"])
            self.assertEqual(shadow.await_args.kwargs["exclude_lead_id"],1)
            self.assertTrue(all(c.args[0] in bot.settings.admin_ids
                for c in fake_bot.send_message.await_args_list))
