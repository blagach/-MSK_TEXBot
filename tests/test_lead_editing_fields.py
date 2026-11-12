"""C01/B05: each edit control uses the actual handler and real in-memory FSM."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

class LeadEditingFieldsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global bot
        os.environ.setdefault('BOT_TOKEN','123456:TEST')
        os.environ.setdefault('ADMIN_IDS','1,2')
        os.environ.setdefault('LEADS_CHANNEL_ID','-100123')
        import bot
        self.state=FSMContext(MemoryStorage(),StorageKey(bot_id=99,chat_id=12,user_id=12))
        self.message=MagicMock(spec=Message)
        self.message.message_id=44
        self.message.chat=SimpleNamespace(id=12,type="private")
        self.message.bot=SimpleNamespace(edit_message_reply_markup=AsyncMock())
        self.message.answer=AsyncMock(return_value=SimpleNamespace(message_id=44))
        self.original={'equipment':'мини-экскаватор','equipment_name':'','region':'Москва','area':'ЦАО','place':'Адрес','map_link':'https://yandex.ru/maps/','work_date':'25.12.2099 09:00','schedule':'Смена','details':'Описание','attachments':['Вилы'],'payment_method':'Любая форма оплаты','phone':'+79990000001','draft_id':'edit-all','form_keyboard_id':44}
    def callback(self,data,message_id=44):
        self.message.message_id=message_id
        return SimpleNamespace(data=data,message=self.message,from_user=SimpleNamespace(id=12),answer=AsyncMock())
    async def test_every_edit_field_enters_expected_step_and_preserves_all_values(self):
        for i,field in enumerate(bot.LEAD_EDIT_FIELDS):
            with self.subTest(field=field.state):
                await self.state.set_state(bot.LeadForm.edit)
                await self.state.set_data(dict(self.original))
                await bot.lead_review_action(self.callback(f'lead_review:{i}'),self.state,SimpleNamespace())
                self.assertEqual(await self.state.get_state(),field.state)
                data=await self.state.get_data()
                for key,value in self.original.items(): self.assertEqual(data[key],value)
                self.assertEqual(data['lead_editing'],field.state)
    async def test_retired_edit_panel_cannot_change_current_data(self):
        await self.state.set_state(bot.LeadForm.edit)
        await self.state.set_data(dict(self.original))
        c=self.callback('lead_review:3',message_id=43)
        await bot.lead_review_action(c,self.state,SimpleNamespace())
        self.assertEqual(await self.state.get_data(),self.original)
        self.assertEqual(await self.state.get_state(),bot.LeadForm.edit.state)
    async def test_map_skip_from_edit_returns_to_review_without_losing_address(self):
        await self.state.set_state(bot.LeadForm.map_link)
        await self.state.set_data({**self.original,'lead_editing':bot.LeadForm.map_link.state})
        await bot.skip_lead_optional(self.callback('lead_optional_skip'),self.state)
        data=await self.state.get_data()
        self.assertEqual(await self.state.get_state(),bot.LeadForm.review.state)
        self.assertEqual(data['map_link'],'')
        self.assertEqual(data['place'],self.original['place'])
        self.assertEqual(data['phone'],self.original['phone'])
        self.assertIsNone(data['lead_editing'])
