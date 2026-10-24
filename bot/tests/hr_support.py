"""Shared helpers of the HR tests: a throwaway Postgres database migrated with the api's alembic, a seeded dataset,
fake aiogram messages / callbacks. Nothing here talks to Telegram.

Database: ``TEST_HR_DATABASE_URL`` or the api's ``TEST_DATABASE_URL`` (default
postgresql+asyncpg://mosprom:mosprom@127.0.0.1:55433/mosprom_test) with the database name replaced by
``mosprom_test_hr`` (created on demand, schema reset + ``alembic upgrade head`` once per session). Tests are skipped
with a reason when Postgres is unreachable.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
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
from app.hr import outbox, runtime
from app.hr.format import QUIZ_QUESTION_IDS

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "api"
HR_DB = "mosprom_test_hr"
BASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://mosprom:mosprom@127.0.0.1:55433/mosprom_test")
HR_URL = os.environ.get("TEST_HR_DATABASE_URL") or BASE_URL.rsplit("/", 1)[0] + "/" + HR_DB
ADMIN = 1001
ADMIN2 = 1002
STRANGER = 9999

_ready: str | None = None


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _api_python() -> str:
    for cand in (API_DIR / ".venv" / "Scripts" / "python.exe", API_DIR / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable


def ensure_db() -> str:
    """Create / reset / migrate the HR test database once per session; skip the test when Postgres is absent."""
    global _ready
    if _ready:
        return _ready

    async def reset() -> None:
        admin = _dsn(HR_URL.rsplit("/", 1)[0] + "/postgres")
        conn = await asyncpg.connect(admin, timeout=3)
        try:
            name = HR_URL.rsplit("/", 1)[1]
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
                await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()
        conn = await asyncpg.connect(_dsn(HR_URL), timeout=3)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    try:
        asyncio.run(reset())
    except Exception as e:
        pytest.skip(f"Postgres not reachable for the HR tests ({type(e).__name__}): start mosprom-test-pg, see api/tests/conftest.py")
    done = subprocess.run([_api_python(), "-m", "alembic", "upgrade", "head"], cwd=API_DIR, capture_output=True, text=True,
                          env={**os.environ, "DATABASE_URL": HR_URL})
    if done.returncode:
        pytest.skip(f"alembic upgrade head failed for the HR test db: {done.stderr[-400:]}")
    _ready = HR_URL
    return _ready


def make_settings(**kw: Any) -> Settings:
    base = dict(database_url=HR_URL, hr_admin_tg_ids=frozenset({ADMIN, ADMIN2}), bots_enabled=True)
    base.update(kw)
    return Settings(**base)


def make_bot(*, fail: BaseException | None = None) -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=fail) if fail else AsyncMock(return_value=SimpleNamespace(message_id=1))
    return bot


def run_db(fn: Callable[[async_sessionmaker[AsyncSession]], Awaitable[Any]], *, seed: bool = True,
           settings: Settings | None = None, bot: Any = None) -> Any:
    """Run ``fn(sm)`` on a clean (optionally seeded) database with the HR runtime set up on that sessionmaker."""
    url = ensure_db()

    async def go() -> Any:
        engine = create_async_engine(url, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        outbox._next_try.clear()
        try:
            async with sm() as s, s.begin():
                await s.execute(text("TRUNCATE users, seasons, outbox, events RESTART IDENTITY CASCADE"))
            if seed:
                await seed_all(sm)
            runtime.setup(settings or make_settings(), bot, sm)
            return await fn(sm)
        finally:
            runtime._rt = None
            await engine.dispose()

    return asyncio.run(go())


# ---------------------------------------------------------------- dataset

NOW = datetime.now(timezone.utc)
SEASON_START = NOW - timedelta(days=2)


async def x(s: AsyncSession, sql: str, /, **p: Any) -> Any:
    r = await s.execute(text(sql), p)
    return r.scalar() if r.returns_rows else None


async def add_user(s: AsyncSession, tg: int, name: str, *, source: str | None = None, bot: bool = False,
                   seen: datetime | None = None, created: datetime | None = None, username: str | None = None) -> int:
    return await x(s, "INSERT INTO users (tg_id, first_name, username, source, is_bot, last_seen_at, created_at) "
                      "VALUES (:tg, :n, :un, :src, :bot, :seen, :created) RETURNING id",
                   tg=tg, n=name, un=username, src=source, bot=bot, seen=seen or NOW, created=created or NOW)


async def add_factory(s: AsyncSession, uid: int, season: int, name: str, stars: int, uni: str | None, bot: bool = False) -> None:
    await x(s, "INSERT INTO factories (user_id, season_id, name, state, stars, university, is_bot) "
               "VALUES (:u, :s, :n, CAST('{}' AS jsonb), :st, :uni, :bot)", u=uid, s=season, n=name, st=stars, uni=uni, bot=bot)


async def add_lead(s: AsyncSession, uid: int, season: int, name: str, uni: str, *, status: str = "new",
                   tags: list[str] | None = None, revoked: bool = False, created: datetime | None = None, **kw: Any) -> int:
    cols = {"phone": None, "email": None, "telegram": None, "institution_kind": None, "direction": None, "course": None, "vacancy": None,
            "readiness": None, "practice_type": None, "best_profession": None, "season_place": None, "quiz_score": None,
            "birth_date": None, "ready_from": None, "hr_note": None}
    cols.update(kw)
    return await x(s, """INSERT INTO leads (user_id, season_id, full_name, university, status, tags, consent, consent_at,
        consent_text_version, consent_revoked_at, created_at, updated_at, phone, email, telegram, institution_kind, direction,
        course, vacancy,
        readiness, practice_type, best_profession, season_place, quiz_score, birth_date, ready_from, hr_note)
        VALUES (:u, :s, :n, :uni, :st, CAST(:tags AS text[]), true, :now, 'v1', :rev, :created, :created, :phone, :email,
        :telegram, :institution_kind, :direction, :course, :vacancy, :readiness, :practice_type, :best_profession, :season_place, :quiz_score,
        :birth_date, :ready_from, :hr_note) RETURNING id""",
                   u=uid, s=season, n=name, uni=uni, st=status, tags=tags or [], now=NOW,
                   rev=NOW if revoked else None, created=created or NOW, **cols)


async def add_events(s: AsyncSession, uid: int, names: list[str], ts: datetime | None = None) -> None:
    for n in names:
        await x(s, "INSERT INTO events (user_id, name, ts) VALUES (:u, :n, :ts)", u=uid, n=n, ts=ts or NOW)


async def add_shift(s: AsyncSession, uid: int, season: int, created: datetime | None = None) -> None:
    await x(s, "INSERT INTO shifts (user_id, season_id, building_id, profession, game_id, result, score, duration_ms, created_at) "
               "VALUES (:u, :s, 'b1', 'operator', 'g1', 'win', 1, 5000, :c)", u=uid, s=season, c=created or NOW)


OLD_QUIZ_IDS = ("q1978", "q1979", "q1996", "q1999", "q2000", "q2003", "q2005", "q2013", "q2015", "q2022")


async def add_quiz(s: AsyncSession, uid: int, correct: int, wrong: int, ids: tuple[str, ...] = QUIZ_QUESTION_IDS) -> None:
    for i in range(correct + wrong):
        await x(s, "INSERT INTO quiz_answers (user_id, question_id, correct, answer) VALUES (:u, :q, :c, 0)",
                u=uid, q=ids[i], c=i < correct)


async def seed_all(sm: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Season 1 (closed, old), season 2 (current). Humans u1..u5, demo bots b1, b2. See test_hr.py for the numbers."""
    ids: dict[str, int] = {}
    async with sm() as s, s.begin():
        await x(s, "INSERT INTO seasons (starts_at, ends_at, days, closed) VALUES (:a, :b, 7, true)",
                a=NOW - timedelta(days=20), b=NOW - timedelta(days=13))
        await x(s, "INSERT INTO seasons (starts_at, ends_at, days, closed) VALUES (:a, :b, 7, false)",
                a=SEASON_START, b=NOW + timedelta(days=5))
        ids["u1"] = await add_user(s, 11, "Анна", source="ref_1", username="anna")
        ids["u2"] = await add_user(s, 12, "Борис", source="ref_1")
        ids["u3"] = await add_user(s, 13, "Вера", source="qr_hall", seen=NOW - timedelta(days=1))
        ids["u4"] = await add_user(s, 14, "Глеб", source=None, seen=NOW - timedelta(days=3))
        ids["u5"] = await add_user(s, 15, "Дина", source="hr_spring")
        ids["b1"] = await add_user(s, -1000001, "Бот1", source="ref_1", bot=True)
        ids["b2"] = await add_user(s, -1000002, "Бот2", bot=True)
        # factories: season 2 is current; u5 still sits in season 1 (not moved yet) and must not be in the current top
        await add_factory(s, ids["u1"], 2, "Цех Анны", 50, "МГУПП")
        await add_factory(s, ids["u2"], 2, "Цех Бориса", 80, "РХТУ")
        await add_factory(s, ids["u3"], 2, "Цех Веры", 30, "МГУПП")
        await add_factory(s, ids["u4"], 2, "Цех Глеба", 10, None)
        await add_factory(s, ids["u5"], 1, "Цех Дины", 999, "МГУПП")
        await add_factory(s, ids["b1"], 2, "Бот-цех", 100, "МГУПП", bot=True)
        await add_factory(s, ids["b2"], 2, "Бот-цех 2", 60, "РХТУ", bot=True)
        # events (funnel): u1 goes all the way, u2 stops at game_start, u3/u4 only open; duplicates and a bot are ignored
        await add_events(s, ids["u1"], ["app_open", "app_open", "session_start", "game_start", "game_end", "finale_view",
                                        "lead_form_open", "lead_submit"])
        await add_events(s, ids["u2"], ["app_open", "session_start", "game_start", "lead_form_open"])
        await add_events(s, ids["u3"], ["app_open"])
        await add_events(s, ids["u4"], ["app_open"])
        await add_events(s, ids["u5"], ["app_open"], ts=NOW - timedelta(days=10))  # before the current season
        await add_events(s, ids["b1"], ["app_open", "game_start", "lead_submit"])
        # shifts of the current season (u1 x2, u2 x1, one old, one of a bot)
        await add_shift(s, ids["u1"], 2)
        await add_shift(s, ids["u1"], 2)
        await add_shift(s, ids["u2"], 2, created=NOW - timedelta(days=1, hours=5))
        await add_shift(s, ids["b1"], 2)
        # leads: L(u1) new, L(u2) contacted, L(u3) revoked, one by a bot
        ids["l1"] = await add_lead(s, ids["u1"], 2, "Иванова Анна", "МГУПП", tags=["senior", "high_quiz"],
                                   phone="+79991112233", email="anna@example.com", telegram="anna", institution_kind="vuz", direction="Пищевая инженерия",
                                   course="4", vacancy="operator", readiness="season", practice_type="paid",
                                   best_profession="lab", season_place=2, quiz_score=7, birth_date=datetime(2003, 5, 1).date(),
                                   created=NOW - timedelta(hours=3))
        ids["l2"] = await add_lead(s, ids["u2"], 2, "Петров Борис", "РХТУ", status="contacted", tags=["ready_for_season"],
                                   phone="+79990000000", institution_kind="college", direction="Розлив", created=NOW - timedelta(hours=2))
        ids["l3"] = await add_lead(s, ids["u3"], 2, "Сидорова Вера", "МГУПП", tags=["senior"], revoked=True,
                                   phone="+79993334455", created=NOW - timedelta(hours=1))
        ids["lb"] = await add_lead(s, ids["b1"], 2, "Бот Ботов", "МГУПП", created=NOW)
        # quiz: u1 finished (7 of 8 right), u2 started, a bot finished; u4 only has answers of the old S5 bank (not counted)
        await add_quiz(s, ids["u1"], 7, 1)
        await add_quiz(s, ids["u2"], 2, 1)
        await add_quiz(s, ids["b1"], 8, 0)
        await add_quiz(s, ids["u4"], 10, 0, ids=OLD_QUIZ_IDS)
        await add_quiz(s, ids["u2"], 5, 0, ids=OLD_QUIZ_IDS[:5])
    return ids


# ---------------------------------------------------------------- fake aiogram objects

def fake_message(tg_id: int, args: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(from_user=SimpleNamespace(id=tg_id, username="hr", first_name="HR"), answer=AsyncMock(),
                           answer_document=AsyncMock(), edit_text=AsyncMock(), chat=SimpleNamespace(id=tg_id))


def fake_callback(tg_id: int, data: str) -> SimpleNamespace:
    msg = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    return SimpleNamespace(from_user=SimpleNamespace(id=tg_id, username="hr_user", first_name="HR"), data=data,
                           message=msg, answer=AsyncMock())


def reply_text(m: SimpleNamespace) -> str:
    """Text of the (last) reply of a fake message."""
    return m.answer.call_args.args[0]
