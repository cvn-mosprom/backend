from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from app.config import Settings
from app.handlers.start import (
    PLAY_BUTTON_TEXT,
    build_play_keyboard,
    build_webapp_url,
    is_webapp_url_ok,
    normalize_start_param,
    on_start,
    start_text,
)


def test_normalize_start_param():
    assert normalize_start_param("ref_42") == "ref_42"
    assert normalize_start_param("qr-hall_3") == "qr-hall_3"
    assert normalize_start_param("  hr_spring ") == "hr_spring"
    assert normalize_start_param("a" * 64) == "a" * 64
    for bad in (None, "", "   ", "a" * 65, "x y", "ref&admin=1", "ref?x", "привет", "a/b"):
        assert normalize_start_param(bad) is None, bad


def test_build_webapp_url():
    assert build_webapp_url("https://game.example.com", None) == "https://game.example.com"
    assert build_webapp_url("https://game.example.com", "ref_42") == "https://game.example.com?startapp=ref_42"
    assert (
        build_webapp_url("https://game.example.com/app/?v=2", "qr_1") == "https://game.example.com/app/?v=2&startapp=qr_1"
    )
    # an existing startapp is replaced, not duplicated
    assert build_webapp_url("https://g.example.com/?startapp=old", "new") == "https://g.example.com/?startapp=new"


def test_is_webapp_url_ok():
    assert is_webapp_url_ok("https://game.example.com")
    for bad in ("", "http://localhost", "https://", "game.example.com"):
        assert not is_webapp_url_ok(bad)


def test_keyboard_has_single_webapp_button():
    kb = build_play_keyboard("https://game.example.com?startapp=ref_1")
    assert len(kb.inline_keyboard) == 1 and len(kb.inline_keyboard[0]) == 1
    btn = kb.inline_keyboard[0][0]
    assert btn.text == PLAY_BUTTON_TEXT
    assert btn.web_app is not None and btn.web_app.url == "https://game.example.com?startapp=ref_1"
    assert btn.url is None and btn.callback_data is None


def test_start_text_escapes_name_and_mentions_play():
    text = start_text("<b>Ivan</b>")
    assert "&lt;b&gt;Ivan&lt;/b&gt;" in text and "<b>Ivan</b>" not in text
    assert "Играть" in text
    assert "друг" in start_text(None)
    assert "Играть" not in start_text("Ivan", with_button=False)


def _message(first_name: str = "Ivan") -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(first_name=first_name), answer=AsyncMock())


def test_on_start_sends_webapp_button_with_start_param():
    msg = _message()
    settings = Settings(webapp_url="https://game.example.com")
    asyncio.run(on_start(msg, CommandObject(command="start", args="ref_42"), settings))
    msg.answer.assert_awaited_once()
    (text,), kwargs = msg.answer.await_args
    assert "Ivan" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.web_app.url == "https://game.example.com?startapp=ref_42"


def test_on_start_without_param_and_with_garbage_param():
    for args in (None, "x y z", "ref&evil=1"):
        msg = _message()
        asyncio.run(on_start(msg, CommandObject(command="start", args=args), Settings(webapp_url="https://g.example.com")))
        button = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.web_app.url == "https://g.example.com"


def test_on_start_without_https_url_replies_without_button():
    for url in ("", "http://localhost"):
        msg = _message()
        asyncio.run(on_start(msg, CommandObject(command="start", args="ref_1"), Settings(webapp_url=url)))
        msg.answer.assert_awaited_once()
        assert "reply_markup" not in msg.answer.await_args.kwargs


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, ""),
        ({"DOMAIN": "game.example.com"}, "https://game.example.com/app"),
        ({"DOMAIN": "localhost"}, "https://localhost/app"),
        ({"DOMAIN": "http://localhost"}, "http://localhost/app"),
        ({"DOMAIN": "x.example.com", "WEBAPP_URL": "https://app.example.com/"}, "https://app.example.com"),
    ],
)
def test_settings_webapp_url(env, expected):
    assert Settings.from_env(env).webapp_url == expected


def test_settings_defaults():
    s = Settings.from_env({"BOT_TOKEN": " 123:abc ", "LOG_LEVEL": "debug", "APP_ENV": "Prod"})
    assert (s.bot_token, s.log_level, s.app_env) == ("123:abc", "DEBUG", "prod")
    assert Settings.from_env({}).bot_token == ""
