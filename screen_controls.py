"""Retire private navigation panels without deleting messages or durable cards.

The cursor lives outside the active form, in the existing FSM storage. A failed
Telegram edit must not make an obsolete callback executable again.
"""
import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, replace

from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove


PREFIXES = ("admin:", "paused_action:", "sb:", "co:", "sc:", "lab:", "provider_alert:", "registration_cancel:", "provider_payment:",
            "machine:", "machine_catalog:", "machine_review:", "machine_field:", "provider_legacy:",
            "form_nav:", "cancel_lead_draft:", "offer_draft:", "lead_step:",
            "lead_attachment:", "lead_review:", "lead_close:", "offer_step:")
LEGACY_PANELS = ("form_keyboard_id", "alert_panel", "payment_panel", "machine_panel")


def temporary(action):
    # Historical lead/contact cards and support ticket links remain
    # usable; their handlers still enforce ownership, permissions and status.
    return bool(action and (action.startswith(PREFIXES)
                or action in {"lead_optional_skip", "lead_skip_details"}
                or (action.startswith("help:") and not action.startswith("help:ticket:"))))


def remaining_markup(markup):
    if not isinstance(markup, InlineKeyboardMarkup):
        return None
    rows = [[button for button in row if not temporary(button.callback_data)]
            for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def managed(markup):
    return isinstance(markup, InlineKeyboardMarkup) and any(
        temporary(button.callback_data) for row in markup.inline_keyboard for button in row
    )


@dataclass
class Screen:
    chat_id: int
    event_id: int
    cursor: FSMContext
    legacy_ids: set[int]
    task: object
    panels: dict
    switched: bool = False


class ScreenControls:
    def __init__(self):
        self.active = ContextVar("private_screen", default=None)

    async def install(self, bot):
        # Also called on the first update, so feed_update and polling use the
        # same path. No API request or background operation is introduced here.
        if self.outgoing not in bot.session.middleware:
            bot.session.middleware(self.outgoing)

    async def remove(self, bot, chat_id, message_id, markup=None):
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
        except TelegramAPIError as error:
            logging.warning("Could not retire screen controls: %s", type(error).__name__)

    async def retire(self, bot, screen, through, panel=None, remaining=None):
        old = await screen.cursor.get_data()
        if panel is not None:
            screen.panels[str(panel)] = remaining
        # Persist the invalidation before trying a best-effort visual edit.
        await screen.cursor.set_data({
            "through": max(old.get("through", 0), through),
            "panels": screen.panels,
            "revision": old.get("revision", 0) + 1,
        })
        previous = old.get("panels", {})
        for message_id, remaining in previous.items():
            if message_id not in screen.panels:
                markup = InlineKeyboardMarkup.model_validate(remaining) if remaining else None
                await self.remove(bot, screen.chat_id, int(message_id), markup)
        for message_id in screen.legacy_ids - {int(value) for value in previous} - {panel}:
            await self.remove(bot, screen.chat_id, message_id)
        screen.legacy_ids.clear()

    async def outgoing(self, make_request, bot, method):
        screen = self.active.get()
        if (screen is None or screen.task is not asyncio.current_task()
                or getattr(method, "chat_id", None) != screen.chat_id):
            return await make_request(bot, method)
        markup = getattr(method, "reply_markup", None)
        new_panel = managed(markup)
        new_keyboard = isinstance(markup, (ReplyKeyboardMarkup, ReplyKeyboardRemove))
        # Ignore edits that only remove buttons, and preserve unrelated messages
        # (validation errors, asynchronous notices, durable object cards).
        if not (new_panel or new_keyboard):
            return await make_request(bot, method)
        # Invalidate before a new send: an ambiguous send or process interruption
        # must not leave the previous screen executable. /menu can recover it.
        if not screen.switched:
            old = await screen.cursor.get_data()
            existing_id = getattr(method, "message_id", None)
            if existing_id is not None and str(existing_id) in old.get("panels", {}):
                # Toggling a selection or updating one machine card keeps the
                # other cards of the same inventory screen usable.
                screen.panels.update(old["panels"])
            else:
                await self.retire(bot, screen, max(screen.event_id, *map(int, old.get("panels", {})),
                                                  *screen.legacy_ids, 0))
            screen.switched = True
        result = await make_request(bot, method)
        message_id = result.message_id if isinstance(result, Message) else getattr(method, "message_id", None)
        if message_id is not None:
            remaining = remaining_markup(markup)
            await self.retire(bot, screen, message_id - 1 if new_panel else message_id,
                              message_id if new_panel else None,
                              remaining.model_dump(mode="json", exclude_none=True) if remaining else None)
        return result

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        state = data.get("state")
        if not isinstance(message, Message) or message.chat.type != "private" or state is None:
            return await handler(event, data)
        bot = data["bot"]
        await self.install(bot)
        cursor = FSMContext(state.storage, replace(state.key, destiny="screen_controls_v1"))
        old = await cursor.get_data()
        if (isinstance(event, CallbackQuery) and temporary(event.data)
                and str(message.message_id) not in old.get("panels", {})
                and message.message_id <= old.get("through", 0)):
            await event.answer("Этот экран устарел. Используйте актуальные кнопки или /menu.")
            await self.remove(bot, message.chat.id, message.message_id, remaining_markup(message.reply_markup))
            return
        form_data = await state.get_data()
        screen = Screen(message.chat.id, message.message_id, cursor, {form_data[key] for key in LEGACY_PANELS
                                                if isinstance(form_data.get(key), int)}, asyncio.current_task(), {})
        initial_state = await state.get_state()
        token = self.active.set(screen)
        try:
            return await handler(event, data)
        finally:
            try:
                current = await cursor.get_data()
                # A text-only next step must retire the previous panel even if
                # its Telegram send failed. Validation errors keep the panel.
                if (await state.get_state() != initial_state
                        and current.get("revision", 0) == old.get("revision", 0)):
                    through = max(message.message_id, *map(int, current.get("panels", {})), *screen.legacy_ids, 0)
                    await self.retire(bot, screen, through)
            finally:
                self.active.reset(token)
