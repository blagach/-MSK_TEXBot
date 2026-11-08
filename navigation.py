"""Shared Telegram keyboards and reversible form steps."""
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

PLACE_REQUEST = "📝 Разместить заявку"
FIND_EQUIPMENT = "🔎 Найти технику"
PROVIDERS = "🚜 Исполнителям"
MY_REQUESTS = "📋 Мои заявки"
ACCOUNT = "👤 Личный кабинет"
HELP = "❓ Помощь и поддержка"
BACK = "⬅️ Назад"
HOME = "🏠 Главное меню"
REGISTER = "🚜 Зарегистрироваться как исполнитель"
PROFILE = "👤 Мой профиль"
PAYMENT = "💳 Способы оплаты"
ADD_MACHINE = "➕ Добавить технику"
MY_MACHINES = "📋 Моя техника"
ENABLE_NOTIFICATIONS = "🔔 Уведомления вкл"
DISABLE_NOTIFICATIONS = "🔕 Уведомления выкл"


def keyboard(*rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_menu():
    return keyboard(
        (PLACE_REQUEST, FIND_EQUIPMENT),
        (PROVIDERS, MY_REQUESTS),
        (ACCOUNT, HELP),
    )


def section_menu(*rows):
    return keyboard(*rows, (BACK, HOME))


PROVIDER_MENU = section_menu(
    (REGISTER,), (PROFILE, PAYMENT), (ADD_MACHINE, MY_MACHINES),
    (ENABLE_NOTIFICATIONS, DISABLE_NOTIFICATIONS),
)
ACCOUNT_MENU = section_menu((MY_REQUESTS, PROFILE), (PROVIDERS,))


class Form:
    def __init__(self, steps):
        self.steps = steps

    async def show(self, message, state, target):
        prompt, *choices = self.steps[target]
        await state.set_state(target)
        await message.answer(prompt, reply_markup=section_menu(*choices))

    def previous(self, current):
        previous = None
        for step in self.steps:
            if step.state == current:
                return previous
            previous = step
        return None

    def contains(self, current):
        return any(step.state == current for step in self.steps)
