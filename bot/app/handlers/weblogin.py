"""S14: web login through the bot (docs/ARCHITECTURE.md, "Web login via Telegram bot").

* ``/login``            -> api ``POST /internal/auth/login-code`` -> the 6-digit code (valid 5 min) for the site's code form.
* ``/start weblogin_<nonce>`` (deep link from the site) -> "Войти на сайт как <name>?" Да / Нет -> ``POST /internal/auth/approve``.
* ``/logout_web``       -> ``POST /internal/auth/revoke-all`` (every web session of this Telegram user).

All three are private-chat only (a code must never land in a group). This router is included BEFORE ``start`` so that
``weblogin_`` payloads never reach the generic /start (start_param pass-through, reminders toggle, users upsert).
Nothing here raises: an api that is down or answers badly gives the player a polite retry text.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from ..config import Settings
from ..jobs import InternalApiError, get_api

log = logging.getLogger(__name__)
router = Router(name="weblogin")

DEEP_LINK_PREFIX = "weblogin_"
# start param <= 64 chars, so nonce <= 55; callback_data "wl:y:<nonce>" then stays <= 60 bytes (Telegram limit 64)
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,55}$")
CB_YES = "wl:y:"
CB_NO = "wl:n"
TIMEOUT_S = 10.0

BAD_LINK_TEXT = "Ссылка для входа повреждена. Нажми «Войти через Telegram» на сайте ещё раз."
TRY_LATER_TEXT = "Сейчас не получается, попробуй чуть позже."
CANCELLED_TEXT = "Вход отменён."
DONE_TEXT = "Готово, вернись на сайт."
APPROVE_ERRORS = {
    410: "Ссылка для входа устарела или уже использована (она действует 5 минут). Нажми «Войти через Telegram» на сайте ещё раз.",
    404: "Такой ссылки для входа нет. Нажми «Войти через Telegram» на сайте ещё раз.",
    409: "Эта ссылка для входа уже использована. Если вход не завершился, запроси новую на сайте.",
    429: "Слишком много попыток, подожди минуту и попробуй снова.",
}


async def api_post(settings: Settings, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    """(http status, json) of one internal call; status 0 when the api is unreachable / not configured. Never raises."""
    try:
        api = get_api(settings)
        if not api.token:
            log.warning("%s skipped: INTERNAL_TOKEN is not set", path)
            return 0, None
        return 200, await api.post(path, payload, TIMEOUT_S)
    except InternalApiError as e:
        log.warning("%s -> HTTP %s", path, e.status)
        return e.status, None
    except Exception as e:  # connection refused (api restarting), timeout, bad JSON
        log.warning("%s failed: %s: %s", path, type(e).__name__, e)
        return 0, None


def profile_payload(user: User) -> dict[str, Any]:
    return {"tg_id": user.id, "username": user.username, "first_name": user.first_name, "last_name": user.last_name}


def display_name(user: User) -> str:
    name = " ".join(p for p in (user.first_name, user.last_name) if p and p.strip()).strip()
    return name or (f"@{user.username}" if user.username else "друг")


def confirm_keyboard(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, войти", callback_data=CB_YES + nonce),
        InlineKeyboardButton(text="Нет", callback_data=CB_NO),
    ]])


def plural_sessions(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} сессия"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} сессии"
    return f"{n} сессий"


# ---- /login ----------------------------------------------------------------------------------------------------------

@router.message(Command("login"), F.chat.type == "private")
async def cmd_login(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    status, data = await api_post(settings, "/internal/auth/login-code", profile_payload(message.from_user))
    code = data.get("code") if data else None
    if status != 200 or not code:
        await message.answer(TRY_LATER_TEXT)
        return
    text = f"Код для входа на сайт: <code>{html.escape(str(code))}</code>\nДействует 5 минут, никому не сообщай."
    if settings.webapp_url:
        text += f"\n\nСайт: {html.escape(settings.webapp_url)}"
    await message.answer(text)


# ---- /start weblogin_<nonce> -----------------------------------------------------------------------------------------

@router.message(CommandStart(deep_link=True, magic=F.args.startswith(DEEP_LINK_PREFIX)), F.chat.type == "private")
async def on_weblogin_start(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    nonce = (command.args or "")[len(DEEP_LINK_PREFIX):]
    if not NONCE_RE.fullmatch(nonce):
        await message.answer(BAD_LINK_TEXT)
        return
    await message.answer(f"Войти на сайт как <b>{html.escape(display_name(message.from_user))}</b>?",
                         reply_markup=confirm_keyboard(nonce))


async def finish(callback: CallbackQuery, text: str) -> None:
    """Replace the question (and drop its buttons); the message may be gone / too old to edit."""
    try:
        if callback.message is not None and hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(text, reply_markup=None)
    except TelegramAPIError as e:
        log.info("weblogin: could not edit the confirmation message: %s", e)
    await callback.answer()


@router.callback_query(F.data.startswith(CB_YES))
async def on_weblogin_yes(callback: CallbackQuery, settings: Settings) -> None:
    nonce = (callback.data or "")[len(CB_YES):]
    if not NONCE_RE.fullmatch(nonce):
        await finish(callback, BAD_LINK_TEXT)
        return
    status, _ = await api_post(settings, "/internal/auth/approve", {"nonce": nonce, **profile_payload(callback.from_user)})
    await finish(callback, DONE_TEXT if status == 200 else APPROVE_ERRORS.get(status, TRY_LATER_TEXT))


@router.callback_query(F.data == CB_NO)
async def on_weblogin_no(callback: CallbackQuery) -> None:
    await finish(callback, CANCELLED_TEXT)


# ---- /logout_web -----------------------------------------------------------------------------------------------------

@router.message(Command("logout_web"), F.chat.type == "private")
async def cmd_logout_web(message: Message, settings: Settings) -> None:
    if message.from_user is None:
        return
    status, data = await api_post(settings, "/internal/auth/revoke-all", {"tg_id": message.from_user.id})
    if status != 200 or data is None:
        await message.answer(TRY_LATER_TEXT)
        return
    n = data.get("revoked", data.get("count", 0))
    n = n if isinstance(n, int) and n > 0 else 0
    await message.answer(f"Вышел из всех веб-сессий: {plural_sessions(n)}." if n else "Активных веб-сессий нет.")
