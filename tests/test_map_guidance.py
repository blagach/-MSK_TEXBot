import os
import unittest
from unittest.mock import AsyncMock, patch
os.environ.setdefault('BOT_TOKEN','123456:TEST')
import bot
import support_content

class MapGuidanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_map_panel_keeps_skip_back_and_existing_address(self):
        state=AsyncMock()
        state.get_data.return_value={'place':'Сохранённый адрес'}
        message=AsyncMock()
        with patch.object(bot,'clear_form_keyboard',AsyncMock()), patch.object(bot,'checkpoint_form',AsyncMock()), patch.object(bot,'send_form_panel',AsyncMock()) as panel:
            await bot.show_lead_step(message,state,bot.LeadForm.map_link)
        rows=panel.await_args.args[3]
        buttons=[b for row in rows for b in row]
        self.assertTrue(any(b.url=='https://yandex.ru/maps/' for b in buttons))
        self.assertTrue(any(b.callback_data=='lead_optional_skip' for b in buttons))
        self.assertTrue(any(b.callback_data=='form_nav:back' for b in buttons))
        self.assertIn('Поделиться',panel.await_args.args[2])
        self.assertNotIn('HTTPS',panel.await_args.args[2])
        state.update_data.assert_not_awaited()

    async def test_invalid_link_does_not_advance_or_clear_address(self):
        message=AsyncMock();message.text='не ссылка'
        state=AsyncMock()
        with patch.object(bot,'advance_lead',AsyncMock()) as advance:
            await bot.lead_map_link(message,state)
        advance.assert_not_awaited()
        state.clear.assert_not_awaited()
        self.assertIn('Пропустить',message.answer.await_args.args[0])
        self.assertIn('Точка на карте',str(support_content.GUIDES['customer']))

    async def test_help_explains_current_contact_visibility_and_notification_navigation(self):
        import support_content
        guide=str(support_content.GUIDES['provider'])
        faq=str(support_content.FAQ)
        self.assertIn('отдельными сообщениями',guide)
        self.assertNotIn('Откройте «🔎 Подходящие заявки»',guide)
        self.assertIn('Как получить контакт заказчика?',faq)
        self.assertIn('длинная анкета отклика не обязательна',faq)
        self.assertIn('зарегистрированные исполнители',faq)
