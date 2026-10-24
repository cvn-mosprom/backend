"""Reminder consent: the inline toggle (callbacks ``notify:on`` / ``notify:off``) and the /notify, /stop commands.

Only players who opted in here (users.notify_opt_in, consent moment in notify_opt_in_at) ever get order reminders. The first
/start creates the users row when the player has not opened the Mini App yet (see store.py).
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from .keyboards import NOTIFY_OFF_DATA, NOTIFY_ON_DATA, notify_button
from . import runtime, store

log = logging.getLogger(__name__)
router = Router(name="notify")

ON_TEXT = ("🔔 Напоминания включены. Напишу, когда заказ почти готов или горит срок "
           "(по ночам, с 23:00 до 09:00 МСК, не беспокою). Отключить: /stop.")
OFF_TEXT = "🔕 Напоминания выключены. Включить снова: /notify."
ALREADY_ON_TEXT = "🔔 Напоминания уже включены. Отключить: /stop."
ALREADY_OFF_TEXT = "🔕 Напоминания и так выключены. Включить: /notify."
UNAVAILABLE_TEXT = "Напоминания сейчас недоступны, попробуй позже."


def profile_of(user: Any) -> store.Profile:
    return store.Profile(tg_id=user.id, username=getattr(user, "username", None),
                         first_name=getattr(user, "first_name", None), last_name=getattr(user, "last_name", None))


async def apply(user: Any, on: bool) -> tuple[bool | None, str]:
    """Switch the consent; returns (changed, reply text). changed is None when the service is unavailable."""
    sm = runtime.get_sessionmaker()
    if sm is None:
        return None, UNAVAILABLE_TEXT
    try:
        if on:
            changed = await store.set_opt_in(sm, profile_of(user))
            return changed, ON_TEXT if changed else ALREADY_ON_TEXT
        changed = await store.set_opt_out(sm, user.id)
        return changed, OFF_TEXT if changed else ALREADY_OFF_TEXT
    except Exception as e:
        log.warning("notify toggle failed for %s: %s: %s", getattr(user, "id", "?"), type(e).__name__, e)
        return None, UNAVAILABLE_TEXT


@router.message(Command("notify"), F.chat.type == "private")
async def cmd_notify(message: Message) -> None:
    if message.from_user is None:
        return
    _, reply = await apply(message.from_user, True)
    await message.answer(reply)


@router.message(Command("stop"), F.chat.type == "private")
async def cmd_stop(message: Message) -> None:
    if message.from_user is None:
        return
    _, reply = await apply(message.from_user, False)
    await message.answer(reply)


def with_toggle(markup: Any, opted_in: bool) -> InlineKeyboardMarkup:
    """The message's keyboard with its toggle row replaced (the Play button row stays)."""
    rows = [r for r in (getattr(markup, "inline_keyboard", None) or [])
            if not any((b.callback_data or "").startswith("notify:") for b in r)]
    rows.append([notify_button(opted_in)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.in_({NOTIFY_ON_DATA, NOTIFY_OFF_DATA}))
async def on_toggle(callback: CallbackQuery) -> None:
    on = callback.data == NOTIFY_ON_DATA
    changed, reply = await apply(callback.from_user, on)
    await callback.answer(reply.split(".")[0][:200], show_alert=False)
    if changed is None:
        return
    if changed:  # the visible state flips with the toggle; a repeated tap keeps the message as it is
        message = callback.message
        if message is not None and hasattr(message, "edit_reply_markup"):
            try:
                await message.edit_reply_markup(reply_markup=with_toggle(getattr(message, "reply_markup", None), on))
            except TelegramAPIError as e:  # "message is not modified", too old to edit ...
                log.debug("toggle keyboard not updated: %s", e)
        if message is not None and hasattr(message, "answer"):
            await message.answer(reply)
