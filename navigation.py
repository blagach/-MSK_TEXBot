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
EDIT_PROFILE = "✏️ Редактировать профиль"
SERVICES = "🧰 Направления услуг"
SHARE_PHONE = "📲 Отправить свой номер"
PROFILE_PHONE = "Использовать номер из профиля"
ADD_MACHINE = "➕ Добавить технику"
MY_MACHINES = "📋 Моя техника"
ENABLE_NOTIFICATIONS = "🔔 Уведомления вкл"
DISABLE_NOTIFICATIONS = "🔕 Уведомления выкл"
NOTIFICATIONS_ON = "🔔 Уведомления: ВКЛЮЧЕНЫ ✅"
NOTIFICATIONS_OFF = "🔕 Уведомления: ВЫКЛЮЧЕНЫ ❌"


def notification_label(enabled):
    return NOTIFICATIONS_ON if enabled else NOTIFICATIONS_OFF


def phone_input(message):
    contact = getattr(message, "contact", None)
    if contact is None:
        return message.text
    if contact.user_id != message.from_user.id:
        return None
    return contact.phone_number



def keyboard(*rows):
    return ReplyKeyboardMarkup(
        keyboard=[[label if isinstance(label, KeyboardButton) else KeyboardButton(text=label) for label in row] for row in rows],
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


def provider_menu(enabled=False):
    return section_menu(
        (REGISTER,), (PROFILE, PAYMENT), (ADD_MACHINE, MY_MACHINES),
        (SERVICES,), (notification_label(enabled),),
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


async def remember_screen(state, screen, default_parent=None):
    history = (await state.get_data()).get("screens") or []
    if not history and default_parent:
        history = [default_parent]
    if screen in history:
        history = history[:history.index(screen) + 1]
    else:
        history = [*history, screen]
    await state.set_data({"screens": history})


async def return_to_provider_menu(state):
    await state.set_state(None)
    await remember_screen(state, PROVIDERS)
