"""Helpers of the player-feature tests: a throwaway Postgres database migrated with the api's alembic (0007 included), seeding,
fake aiogram objects. Nothing here talks to Telegram.

Database: ``TEST_PLAYER_DATABASE_URL`` or the api's ``TEST_DATABASE_URL`` (default
postgresql+asyncpg://mosprom:mosprom@127.0.0.1:55433/mosprom_test) with the database name replaced by
``mosprom_test_player_bot`` (created on demand, schema reset + ``alembic upgrade head`` once per session). The tests needing it
are skipped with a reason when Postgres is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import Settings
from app.hr import runtime as hr_runtime

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "api"
DB_NAME = "mosprom_test_player_bot"
BASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://mosprom:mosprom@127.0.0.1:55433/mosprom_test")
DB_URL = os.environ.get("TEST_PLAYER_DATABASE_URL") or BASE_URL.rsplit("/", 1)[0] + "/" + DB_NAME

_ready: str | None = None


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _api_python() -> str:
    for cand in (API_DIR / ".venv" / "Scripts" / "python.exe", API_DIR / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def ensure_db() -> str:
    global _ready
    if _ready:
        return _ready

    async def reset() -> None:
        conn = await asyncpg.connect(_dsn(DB_URL.rsplit("/", 1)[0] + "/postgres"), timeout=3)
        try:
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", DB_NAME):
                await conn.execute(f'CREATE DATABASE "{DB_NAME}"')
        finally:
            await conn.close()
        conn = await asyncpg.connect(_dsn(DB_URL), timeout=3)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    try:
        asyncio.run(reset())
    except Exception as e:
        pytest.skip(f"Postgres not reachable for the player tests ({type(e).__name__}): start mosprom-test-pg")
    done = subprocess.run([_api_python(), "-m", "alembic", "upgrade", "head"], cwd=API_DIR, capture_output=True, text=True,
                          env={**os.environ, "DATABASE_URL": DB_URL})
    if done.returncode:
        pytest.skip(f"alembic upgrade head failed for the player test db: {done.stderr[-400:]}")
    _ready = DB_URL
    return _ready


def make_settings(**kw: Any) -> Settings:
    base: dict[str, Any] = dict(database_url=DB_URL, webapp_url="https://game.example.com",
                                hr_admin_tg_ids=frozenset({1001, 1002}))
    base.update(kw)
    return Settings(**base)


def make_bot(*, fail: BaseException | list[BaseException | None] | None = None) -> MagicMock:
    bot = MagicMock()
    if isinstance(fail, list):
        bot.send_message = AsyncMock(side_effect=[f if f is not None else SimpleNamespace(message_id=1) for f in fail]
                                     + [SimpleNamespace(message_id=1)] * 50)
    elif fail is not None:
        bot.send_message = AsyncMock(side_effect=fail)
    else:
        bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
    return bot


def run_db(fn: Callable[[async_sessionmaker[AsyncSession]], Awaitable[Any]], *, settings: Settings | None = None,
           bot: Any = None) -> Any:
    """Run ``fn(sm)`` on an empty database with the HR runtime (bot + settings + sessionmaker) set up on it."""
    url = ensure_db()

    async def go() -> Any:
        engine = create_async_engine(url, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sm() as s, s.begin():
                await s.execute(text("TRUNCATE users, seasons RESTART IDENTITY CASCADE"))
                await s.execute(text("INSERT INTO seasons (starts_at, ends_at, days) "
                                     "VALUES (now(), now() + interval '7 days', 7)"))
            hr_runtime.setup(settings or make_settings(), bot, sm)
            return await fn(sm)
        finally:
            hr_runtime._rt = None
            await engine.dispose()

    return asyncio.run(go())


async def add_player(sm: async_sessionmaker[AsyncSession], tg_id: int, *, orders: Any = None, snapshot: Any = "auto",
                     opt_in: bool = True, started: bool = True, is_bot: bool = False, factory: bool = True) -> int:
    """A users row (+ factory with a snapshot holding `orders`); returns users.id."""
    async with sm() as s, s.begin():
        uid = (await s.execute(text(
            "INSERT INTO users (tg_id, first_name, is_bot, notify_opt_in, notify_opt_in_at, bot_started_at) "
            "VALUES (:tg, 'P', :bot, :opt, CASE WHEN :opt THEN now() END, CASE WHEN :st THEN now() END) RETURNING id"),
            {"tg": tg_id, "bot": is_bot, "opt": opt_in, "st": started})).scalar_one()
        if factory:
            snap = {"orders": orders if orders is not None else []} if snapshot == "auto" else snapshot
            await s.execute(text(
                "INSERT INTO factories (user_id, season_id, name, state, snapshot, is_bot) "
                "VALUES (:uid, (SELECT min(id) FROM seasons), 'f', CAST('{}' AS jsonb), CAST(:snap AS jsonb), :bot)"),
                {"uid": uid, "snap": json.dumps(snap) if snap is not None else None, "bot": is_bot})
    return uid


async def fetch(sm: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> list[Any]:
    async with sm() as s:
        return list((await s.execute(text(sql), params)).all())


def order(oid: str, *, now_ms: float, deadline_min: float | None = None, eta_min: float | None = None,
          status: str = "loading", client: str = "Магазин у дома", eta_field: str = "etaAt", **extra: Any) -> dict[str, Any]:
    """An Order of the snapshot with moments given in minutes from `now_ms`."""
    o: dict[str, Any] = {"id": oid, "client": client, "status": status, "qty": 10, "loaded": 4, **extra}
    if deadline_min is not None:
        o["deadline"] = int(now_ms + deadline_min * 60_000)
    if eta_min is not None:
        o[eta_field] = int(now_ms + eta_min * 60_000)
    return o


def fake_user(tg_id: int = 4242, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(id=tg_id, username=kw.get("username", "ivan"), first_name=kw.get("first_name", "Ivan"),
                           last_name=kw.get("last_name"))


def fake_message(user: Any = None, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(from_user=user if user is not None else fake_user(), answer=AsyncMock(),
                           edit_reply_markup=AsyncMock(), **kw)


def fake_callback(data: str, user: Any = None, markup: Any = None) -> SimpleNamespace:
    msg = fake_message(user)
    msg.reply_markup = markup
    return SimpleNamespace(data=data, from_user=msg.from_user, message=msg, answer=AsyncMock())
