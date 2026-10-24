"""Inline keyboards shared by /start, the toggle callbacks and the reminder messages (no imports from handlers: no cycles)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

PLAY_BUTTON_TEXT = "▶ Играть"
NOTIFY_ON_TEXT = "🔔 Напоминать о заказах"  # shown while reminders are off: tap = opt in
NOTIFY_OFF_TEXT = "🔕 Не напоминать"  # shown while they are on: tap = opt out
NOTIFY_ON_DATA = "notify:on"
NOTIFY_OFF_DATA = "notify:off"


def is_webapp_url_ok(url: str) -> bool:
    """Telegram only accepts https:// URLs for WebApp buttons."""
    return url.startswith("https://") and len(url) > len("https://")


def play_button(url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=PLAY_BUTTON_TEXT, web_app=WebAppInfo(url=url))


def notify_button(opted_in: bool) -> InlineKeyboardButton:
    if opted_in:
        return InlineKeyboardButton(text=NOTIFY_OFF_TEXT, callback_data=NOTIFY_OFF_DATA)
    return InlineKeyboardButton(text=NOTIFY_ON_TEXT, callback_data=NOTIFY_ON_DATA)


def build_start_keyboard(url: str | None, opted_in: bool | None) -> InlineKeyboardMarkup | None:
    """Play button (when there is an https Mini App URL) + the reminder toggle (when the state is known, i.e. the DB works)."""
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([play_button(url)])
    if opted_in is not None:
        rows.append([notify_button(opted_in)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
