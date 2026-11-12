"""A11: real FSM transition for an unlisted machine, without network or database."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
import equipment_catalog

class CustomEquipmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        global bot
        os.environ.setdefault("BOT_TOKEN","123456:TEST")
        os.environ.setdefault("ADMIN_IDS","1,2")
        os.environ.setdefault("LEADS_CHANNEL_ID","-100123")
        import bot

    async def test_custom_name_survives_transition_without_losing_saved_address(self):
        state=FSMContext(MemoryStorage(),StorageKey(bot_id=99,chat_id=12,user_id=12))
        await state.set_state(bot.LeadForm.equipment_other)
        await state.set_data({'place':'Сохранённый адрес','area':'Химки','draft_id':'custom-machine'})
        message=SimpleNamespace(text='  Канатная установка  ',answer=AsyncMock(return_value=SimpleNamespace(message_id=55)))
        await bot.lead_equipment_other(message,state)
        data=await state.get_data()
        self.assertEqual(await state.get_state(),bot.LeadForm.region.state)
        self.assertEqual(data['equipment'],equipment_catalog.OTHER)
        self.assertEqual(data['equipment_name'],'Канатная установка')
        self.assertEqual(data['place'],'Сохранённый адрес')
        self.assertEqual(data['draft_id'],'custom-machine')
    async def test_invalid_custom_name_preserves_previous_form(self):
        state=FSMContext(MemoryStorage(),StorageKey(bot_id=99,chat_id=12,user_id=12))
        await state.set_state(bot.LeadForm.equipment_other)
        await state.set_data({'place':'Сохранённый адрес','equipment_name':'Прежнее название'})
        before=await state.get_data()
        for text in (' ', 'x'*121):
            message=SimpleNamespace(text=text,answer=AsyncMock())
            await bot.lead_equipment_other(message,state)
            self.assertEqual(await state.get_state(),bot.LeadForm.equipment_other.state)
            self.assertEqual(await state.get_data(),before)
