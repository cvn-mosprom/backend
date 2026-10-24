"""S14 web login via the bot: /login, the weblogin_<nonce> deep link, /logout_web.

The whole dispatcher runs against a recording fake session (nothing ever reaches Telegram) and a local aiohttp server
that plays the api's /internal/auth/*.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, Update, User
from aiohttp import web
from aiohttp.test_utils import TestServer

from app import jobs
from app.config import Settings
from app.handlers import weblogin
from app.handlers.weblogin import plural_sessions
from app.jobs import InternalApi
from app.main import build_dispatcher

TOKEN = "secret-internal-token"
SITE = "https://game.example.com"
NONCE = "Zx3_k9-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789a"  # 44 chars, like token_urlsafe(32)
USER = User(id=777, is_bot=False, first_name="Иван", last_name="Петров", username="ivan")


class FakeSession(BaseSession):
    """Records every Telegram call instead of making it."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def close(self) -> None: ...

    async def stream_content(self, *a: Any, **k: Any):  # pragma: no cover
        raise NotImplementedError
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append((type(method).__name__, method.model_dump(exclude_none=True)))
        if type(method).__name__ == "SendMessage":
            return Message(message_id=99, date=datetime.now(timezone.utc), chat=Chat(id=777, type="private"), text="x")
        return True

    def sent(self, name: str) -> list[dict[str, Any]]:
        return [c for n, c in self.calls if n == name]


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot(token="123456:ABCdefGhIJKlmNoPQRsTUVwxyZ-0123456", session=session), session


def msg(text: str, chat_type: str = "private") -> Message:
    cmd = text.split()[0]
    return Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=777, type=chat_type), from_user=USER,
                   text=text, entities=[MessageEntity(type="bot_command", offset=0, length=len(cmd))])


def callback(data: str) -> CallbackQuery:
    message = Message(message_id=5, date=datetime.now(timezone.utc), chat=Chat(id=777, type="private"), text="Войти?")
    return CallbackQuery(id="cb1", from_user=USER, chat_instance="ci", data=data, message=message)


def detach_routers() -> None:
    """The routers are module-level singletons that can have one parent only: free them for a fresh dispatcher."""
    from app.handlers import start
    from app.hr.handlers import router as hr_router
    from app.player.notify import router as notify_router

    for r in (start.router, weblogin.router, notify_router, hr_router):
        r._parent_router = None


@pytest.fixture(autouse=True)
def _reset_api():
    jobs.set_api(None)
    detach_routers()
    yield
    jobs.set_api(None)
    detach_routers()


async def with_api(test: Callable[[list[dict[str, Any]], str], Awaitable[None]], *,
                   answers: dict[str, tuple[int, Any]] | None = None) -> None:
    """Fake api: `answers` maps a path to (status, json body); every request is recorded."""
    seen: list[dict[str, Any]] = []
    table = {"/internal/auth/login-code": (200, {"code": "123456", "expiresAt": "2026-10-24T12:05:00Z"}),
             "/internal/auth/approve": (200, {"ok": True}),
             "/internal/auth/revoke-all": (200, {"revoked": 2})}
    table.update(answers or {})

    async def handler(request: web.Request) -> web.Response:
        seen.append({"path": request.path, "token": request.headers.get("X-Internal-Token"), "body": json.loads(await request.read())})
        status, body = table[request.path]
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        await test(seen, str(server.make_url("")).rstrip("/"))
    finally:
        await server.close()


async def feed(update: Update, settings: Settings | None = None) -> FakeSession:
    bot, session = make_bot()
    detach_routers()
    await build_dispatcher(settings or Settings(webapp_url=SITE, internal_token=TOKEN)).feed_update(bot, update)
    return session


def message_update(text: str, chat_type: str = "private") -> Update:
    return Update(update_id=1, message=msg(text, chat_type))


def callback_update(data: str) -> Update:
    return Update(update_id=2, callback_query=callback(data))


def run(coro) -> None:
    asyncio.run(coro)


# ---- /login ----------------------------------------------------------------------------------------------------------

def test_login_replies_with_the_code_and_the_site_link():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        session = await feed(message_update("/login"))
        (sent,) = session.sent("SendMessage")
        assert "<code>123456</code>" in sent["text"]
        assert "5 минут" in sent["text"] and "никому не сообщай" in sent["text"] and SITE in sent["text"]
        assert seen == [{"path": "/internal/auth/login-code", "token": TOKEN,
                         "body": {"tg_id": 777, "username": "ivan", "first_name": "Иван", "last_name": "Петров"}}]

    run(with_api(test))


def test_login_is_private_chats_only():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        for chat_type in ("group", "supergroup", "channel"):
            session = await feed(message_update("/login", chat_type))
            assert session.calls == []
        assert seen == []

    run(with_api(test))


def test_login_api_error_gives_a_polite_text_without_a_code():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        session = await feed(message_update("/login"))
        (sent,) = session.sent("SendMessage")
        assert sent["text"] == weblogin.TRY_LATER_TEXT and "123456" not in sent["text"]

    run(with_api(test, answers={"/internal/auth/login-code": (500, {"detail": "boom"})}))


def test_login_api_down_or_token_missing_gives_a_polite_text():
    async def down() -> None:
        jobs.set_api(InternalApi("http://127.0.0.1:1", TOKEN))  # connection refused
        (sent,) = (await feed(message_update("/login"))).sent("SendMessage")
        assert sent["text"] == weblogin.TRY_LATER_TEXT
        jobs.set_api(InternalApi("http://127.0.0.1:1", ""))  # INTERNAL_TOKEN not configured
        (sent,) = (await feed(message_update("/login"))).sent("SendMessage")
        assert sent["text"] == weblogin.TRY_LATER_TEXT

    run(down())


# ---- /start weblogin_<nonce> -----------------------------------------------------------------------------------------

def test_deep_link_asks_for_confirmation_with_yes_no_buttons():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        session = await feed(message_update(f"/start weblogin_{NONCE}"))
        (sent,) = session.sent("SendMessage")
        assert "Войти на сайт как" in sent["text"] and "Иван Петров" in sent["text"]
        yes, no = sent["reply_markup"]["inline_keyboard"][0]
        assert yes["callback_data"] == f"wl:y:{NONCE}" and no["callback_data"] == "wl:n"
        assert len(yes["callback_data"].encode()) <= 64
        assert seen == []  # nothing is approved before the player taps "Да"

    run(with_api(test))


def test_deep_link_with_a_broken_nonce_is_refused():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        for text in ("/start weblogin_", f"/start weblogin_{'a' * 56}"):
            (sent,) = (await feed(message_update(text))).sent("SendMessage")
            assert sent["text"] == weblogin.BAD_LINK_TEXT and "reply_markup" not in sent
        assert seen == []

    run(with_api(test))


def test_approve_yes_calls_the_api_and_edits_the_message():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        session = await feed(callback_update(f"wl:y:{NONCE}"))
        assert seen == [{"path": "/internal/auth/approve", "token": TOKEN,
                         "body": {"nonce": NONCE, "tg_id": 777, "username": "ivan", "first_name": "Иван", "last_name": "Петров"}}]
        (edit,) = session.sent("EditMessageText")
        assert edit["text"] == weblogin.DONE_TEXT == "Готово, вернись на сайт." and "reply_markup" not in edit
        assert session.sent("AnswerCallbackQuery")

    run(with_api(test))


def test_approve_no_cancels_without_calling_the_api():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        session = await feed(callback_update("wl:n"))
        assert seen == []
        (edit,) = session.sent("EditMessageText")
        assert edit["text"] == "Вход отменён."

    run(with_api(test))


@pytest.mark.parametrize("status,needle", [(410, "устарела"), (404, "нет"), (409, "уже использована"), (500, "попробуй чуть позже")])
def test_approve_errors_are_mapped_to_clear_texts(status, needle):
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        session = await feed(callback_update(f"wl:y:{NONCE}"))
        (edit,) = session.sent("EditMessageText")
        assert needle in edit["text"] and edit["text"] != weblogin.DONE_TEXT

    run(with_api(test, answers={"/internal/auth/approve": (status, {"detail": "x"})}))


def test_approve_with_api_down_says_try_later():
    async def test() -> None:
        jobs.set_api(InternalApi("http://127.0.0.1:1", TOKEN))
        (edit,) = (await feed(callback_update(f"wl:y:{NONCE}"))).sent("EditMessageText")
        assert edit["text"] == weblogin.TRY_LATER_TEXT

    run(test())


# ---- the generic /start keeps working --------------------------------------------------------------------------------

@pytest.mark.parametrize("text,startapp", [("/start", None), ("/start ref_42", "ref_42"), ("/start qr-hall_3", "qr-hall_3"),
                                           ("/start weblogin", "weblogin"), ("/start hr_spring", "hr_spring")])
def test_other_start_payloads_still_open_the_play_button(text, startapp):
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        (sent,) = (await feed(message_update(text))).sent("SendMessage")
        assert "Смена на заводе" in sent["text"] and "Войти на сайт" not in sent["text"]
        button = sent["reply_markup"]["inline_keyboard"][0][0]
        assert button["web_app"]["url"] == (SITE if startapp is None else f"{SITE}?startapp={startapp}")
        assert seen == []  # the web-login api is never touched

    run(with_api(test))


# ---- /logout_web -----------------------------------------------------------------------------------------------------

def test_logout_web_reports_the_revoked_count():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        (sent,) = (await feed(message_update("/logout_web"))).sent("SendMessage")
        assert "2 сессии" in sent["text"]
        assert seen == [{"path": "/internal/auth/revoke-all", "token": TOKEN, "body": {"tg_id": 777}}]

    run(with_api(test))


def test_logout_web_with_no_sessions_group_chat_and_api_error():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        (sent,) = (await feed(message_update("/logout_web"))).sent("SendMessage")
        assert sent["text"] == "Активных веб-сессий нет."
        assert (await feed(message_update("/logout_web", "group"))).calls == []

    run(with_api(test, answers={"/internal/auth/revoke-all": (200, {"revoked": 0})}))

    async def failing(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        (sent,) = (await feed(message_update("/logout_web"))).sent("SendMessage")
        assert sent["text"] == weblogin.TRY_LATER_TEXT

    run(with_api(failing, answers={"/internal/auth/revoke-all": (500, {})}))


def test_plural_sessions():
    assert [plural_sessions(n) for n in (1, 2, 5, 11, 12, 21, 22, 25)] == [
        "1 сессия", "2 сессии", "5 сессий", "11 сессий", "12 сессий", "21 сессия", "22 сессии", "25 сессий"]
