"""/start: greet and offer a WebApp button that opens the Mini App.

Deep links ``https://t.me/<bot>?start=<param>`` arrive as ``/start <param>``. The param is passed through
to the Mini App as ``?startapp=<param>`` (the frontend reads ``Telegram.WebApp.initDataUnsafe.start_param``
and falls back to the ``startapp`` query parameter, see vendor/frontend web/src/lib/telegram.ts), so the
acquisition source (ref_<id>, qr_<place>, hr_<campaign>, ...) survives the hop through the bot.
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from ..config import Settings

log = logging.getLogger(__name__)
router = Router(name="start")

# Telegram allows A-Z a-z 0-9 _ - and up to 64 chars in start parameters.
START_PARAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PLAY_BUTTON_TEXT = "▶ Играть"


def normalize_start_param(raw: str | None) -> str | None:
    """Return the start parameter if it is valid, else None (never forward garbage to the Mini App)."""
    if not raw:
        return None
    raw = raw.strip()
    return raw if START_PARAM_RE.fullmatch(raw) else None


def build_webapp_url(base: str, start_param: str | None) -> str:
    """Mini App URL with ``startapp=<start_param>`` merged into the existing query string."""
    if not start_param:
        return base
    parts = urlsplit(base)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "startapp"]
    query.append(("startapp", start_param))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def is_webapp_url_ok(url: str) -> bool:
    """Telegram only accepts https:// URLs for WebApp buttons."""
    return url.startswith("https://") and len(url) > len("https://")


def build_play_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=PLAY_BUTTON_TEXT, web_app=WebAppInfo(url=url))]]
    )


def start_text(first_name: str | None, *, with_button: bool = True) -> str:
    name = html.escape(first_name.strip()) if first_name and first_name.strip() else "друг"
    head = f"Привет, {name}! Это <b>Смена на заводе</b>: собери свой цех, запусти линию и попади в топ."
    if with_button:
        return head + "\n\nЖми «Играть», чтобы открыть игру."
    return head + "\n\nИгра пока недоступна, попробуй позже."


@router.message(CommandStart(), F.chat.type == "private")
async def on_start(message: Message, command: CommandObject, settings: Settings) -> None:
    start_param = normalize_start_param(command.args)
    first_name = message.from_user.first_name if message.from_user else None
    if not is_webapp_url_ok(settings.webapp_url):
        log.warning("WEBAPP_URL is not an https URL (%r): replying without the Play button", settings.webapp_url)
        await message.answer(start_text(first_name, with_button=False))
        return
    url = build_webapp_url(settings.webapp_url, start_param)
    await message.answer(start_text(first_name), reply_markup=build_play_keyboard(url))
