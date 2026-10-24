"""C2: achievement notifications from the outbox (app/player/achievements.py): only gold or seasonal achievements, only to players who
started the bot and kept the notifications on, never for bots / test users, quiet hours, stale rows, failures, blocked bot. Aiogram
objects are fakes; the database tests need the throwaway Postgres (tests/player_support.py) and are skipped without it."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import text

from app import jobs
from app.player import achievements as ach
from app.player import runtime
from player_support import add_player, fetch, make_bot, make_settings, run_db

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # 15:00 MSK
GOLD = dict(tier="gold", seasonal=False, title="Без брака", points=30)
SEASONAL = dict(tier="silver", seasonal=True, title="Топ-50 сезона", points=30)
SILVER = dict(tier="silver", seasonal=False, title="Пять напитков", points=20)
BRONZE = dict(tier="bronze", seasonal=False, title="Первый заказ", points=10)


@pytest.fixture(autouse=True)
def _reset_backoff():
    ach._next_try.clear()
    yield
    ach._next_try.clear()


async def add_row(sm, uid: int | None, *, kind: str = ach.KIND, age_h: float = 0, via: str = "hook", **kw) -> int:
    payload = {"user_id": uid, "achievement_id": "x", "season_id": 1, "via": via, **{**GOLD, **kw}}
    async with sm() as s, s.begin():
        return (await s.execute(text(
            "INSERT INTO outbox (kind, payload, created_at) VALUES (:k, CAST(:p AS jsonb), :at) RETURNING id"),
            {"k": kind, "p": json.dumps(payload), "at": NOW - timedelta(hours=age_h)})).scalar_one()


async def mark(sm, uid: int, **cols) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    async with sm() as s, s.begin():
        await s.execute(text(f"UPDATE users SET {sets} WHERE id = :uid"), {**cols, "uid": uid})


async def state_of(sm, rid: int):
    (row,) = await fetch(sm, "SELECT sent_at, attempts, last_error FROM outbox WHERE id = :id", id=rid)
    return row


def sent_to(bot) -> list[int]:
    return sorted(c.args[0] for c in bot.send_message.await_args_list)


# ---------------------------------------------------------------- pure

def test_only_gold_or_seasonal_is_notifiable():
    assert ach.notifiable({"tier": "gold", "seasonal": False}) and ach.notifiable({"tier": "silver", "seasonal": True})
    assert ach.notifiable({"tier": "gold", "seasonal": True}) and ach.notifiable({"tier": "bronze", "seasonal": True})
    assert not ach.notifiable({"tier": "silver", "seasonal": False}) and not ach.notifiable({"tier": "bronze"})
    assert not ach.notifiable({"tier": "gold ", "seasonal": "yes"}) and not ach.notifiable({})


def user(**kw):
    base = dict(uid=1, tg_id=1001, is_bot=False, is_test=False, notify_opt_in=True, bot_started_at=NOW)
    return SimpleNamespace(**{**base, **kw})


def test_skip_reasons_in_order():
    p = {"title": "T", **GOLD}
    assert ach.skip_reason(p, user(), NOW, NOW) is None
    assert ach.skip_reason(None, user(), NOW, NOW) == "malformed payload" and ach.skip_reason({"tier": "gold"}, user(), NOW, NOW) == "malformed payload"
    assert ach.skip_reason(p, None, NOW, NOW) == "no such user" and ach.skip_reason(p, user(uid=None), NOW, NOW) == "no such user"
    assert ach.skip_reason(p, user(is_bot=True), NOW, NOW) == "bot or test user"
    assert ach.skip_reason(p, user(is_test=True), NOW, NOW) == "bot or test user"
    assert ach.skip_reason({**p, "tier": "silver"}, user(), NOW, NOW) == "not gold or seasonal"
    assert ach.skip_reason({**p, "tier": "silver", "seasonal": True}, user(), NOW, NOW) is None
    assert ach.skip_reason(p, user(notify_opt_in=False), NOW, NOW) == "not opted in"
    assert ach.skip_reason(p, user(bot_started_at=None), NOW, NOW) == "not opted in"
    assert ach.skip_reason(p, user(tg_id=-5), NOW, NOW) == "no chat"
    # the stale boundary: exactly 48 h is still sent, a second more is not
    assert ach.skip_reason(p, user(), NOW - timedelta(hours=48), NOW) is None
    assert ach.skip_reason(p, user(), NOW - timedelta(hours=48, seconds=1), NOW) == "stale"


def test_message_text_escapes_html_and_names_the_points():
    t = ach.message_text({"title": "<b>Топ</b> & co", "tier": "gold", "points": 60, "seasonal": True})
    assert "&lt;b&gt;Топ&lt;/b&gt; &amp; co" in t and "+60 ОД" in t and "сезонное" in t and "золото" in t and "/stop" in t
    plain = ach.message_text({"title": "Без брака", "tier": "gold", "points": 30, "seasonal": False})
    assert "сезонное" not in plain and "+30 ОД" in plain
    odd = ach.message_text({"title": "T", "tier": "weird", "points": True})
    assert "ОД" not in odd  # a bool is not a number of points


def test_backoff_doubles_up_to_the_cap():
    assert [ach.backoff_s(n) for n in (1, 2, 3, 6, 50)] == [10, 20, 40, 300, 300]


# ---------------------------------------------------------------- the filter on a real outbox

def test_only_gold_or_seasonal_to_opted_in_players_who_started_the_bot():
    async def test(sm):
        a = await add_player(sm, 101)  # opted in, started
        b = await add_player(sm, 102, started=False)  # opted in but never started the bot
        c = await add_player(sm, 103, opt_in=False)  # started, notifications off
        d = await add_player(sm, 104)
        e = await add_player(sm, 105)
        f = await add_player(sm, 106)  # test user
        g = await add_player(sm, 107, is_bot=True)  # demo bot
        await mark(sm, f, is_test=True)
        rows = {
            "gold": await add_row(sm, a, **GOLD),
            "no_start": await add_row(sm, b, **GOLD),
            "off": await add_row(sm, c, **GOLD),
            "silver": await add_row(sm, d, **SILVER),
            "bronze": await add_row(sm, d, **BRONZE),
            "seasonal": await add_row(sm, e, **SEASONAL),
            "test": await add_row(sm, f, **GOLD),
            "bot": await add_row(sm, g, **GOLD),
            "ghost": await add_row(sm, 999_999, **GOLD),
            "garbage": await add_row(sm, "not a number", **GOLD),
        }
        other = await add_row(sm, a, kind="lead_created", **GOLD)  # not ours: never touched
        bot = make_bot()
        stats = await ach.run_notifications(bot, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats == {"sent": 2, "skipped": 8, "failed": 0, "blocked": 0, "quiet": 0}
        assert sent_to(bot) == [101, 105]
        first = next(c_ for c_ in bot.send_message.await_args_list if c_.args[0] == 101)
        assert "Без брака" in first.args[1] and "+30 ОД" in first.args[1]
        buttons = [b_ for row in first.kwargs["reply_markup"].inline_keyboard for b_ in row]
        assert any(b_.web_app for b_ in buttons)  # the Play button
        assert (await state_of(sm, rows["gold"])).sent_at is not None and (await state_of(sm, rows["gold"])).last_error is None
        reasons = {k: (await state_of(sm, rid)).last_error for k, rid in rows.items()}
        assert reasons["no_start"] == "not delivered: not opted in" and reasons["off"] == "not delivered: not opted in"
        assert reasons["silver"] == reasons["bronze"] == "not delivered: not gold or seasonal"
        assert reasons["test"] == reasons["bot"] == "not delivered: bot or test user"
        assert reasons["ghost"] == "not delivered: no such user" and reasons["garbage"] == "not delivered: no such user"
        assert all([(await state_of(sm, rid)).sent_at is not None for rid in rows.values()])  # nothing left pending
        assert (await state_of(sm, other)).sent_at is None  # another kind of the outbox is the HR job's business
        bot.send_message.reset_mock()
        again = await ach.run_notifications(bot, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert again["sent"] == 0 and again["skipped"] == 0
        bot.send_message.assert_not_awaited()

    run_db(test)


def test_messages_are_throttled_and_a_stale_row_is_dropped():
    async def test(sm):
        uid = await add_player(sm, 201)
        old = await add_row(sm, uid, age_h=49, **GOLD)
        fresh1, fresh2 = await add_row(sm, uid, age_h=1, **GOLD), await add_row(sm, uid, **SEASONAL)
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        bot = make_bot()
        stats = await ach.run_notifications(bot, sm, make_settings(), now=NOW, sleep=fake_sleep)
        assert stats["sent"] == 2 and stats["skipped"] == 1 and sleeps == [ach.SEND_INTERVAL_S] * 2
        assert (await state_of(sm, old)).last_error == "not delivered: stale"
        assert (await state_of(sm, fresh1)).sent_at is not None and (await state_of(sm, fresh2)).sent_at is not None

    run_db(test)


def test_quiet_hours_keep_the_rows_for_the_morning():
    async def test(sm):
        uid = await add_player(sm, 301)
        rid = await add_row(sm, uid, **GOLD)
        night = datetime(2026, 10, 24, 20, 30, tzinfo=timezone.utc)  # 23:30 MSK
        bot = make_bot()
        stats = await ach.run_notifications(bot, sm, make_settings(), now=night, sleep=AsyncMock())
        assert stats["quiet"] == 1 and stats["sent"] == 0
        bot.send_message.assert_not_awaited()
        assert (await state_of(sm, rid)).sent_at is None  # untouched, not even skipped
        morning = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)  # 09:00 MSK
        stats = await ach.run_notifications(bot, sm, make_settings(), now=morning, sleep=AsyncMock())
        assert stats["sent"] == 1 and sent_to(bot) == [301]

    run_db(test)


# ---------------------------------------------------------------- failures

def test_a_failed_send_is_retried_after_a_backoff_and_does_not_burn_the_other_rows():
    async def test(sm):
        a, b = await add_player(sm, 401), await add_player(sm, 402)
        r1, r2 = await add_row(sm, a, **GOLD), await add_row(sm, b, **GOLD)
        down = make_bot(fail=OSError("network is down"))
        stats = await ach.run_notifications(down, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats["failed"] == 1 and stats["sent"] == 0 and down.send_message.await_count == 1  # stopped after the first failure
        st = await state_of(sm, r1)
        assert st.sent_at is None and st.attempts == 1 and "OSError" in st.last_error
        assert (await state_of(sm, r2)).attempts == 0
        # still inside the backoff: the failed row is skipped, the next one goes out
        up = make_bot()
        stats = await ach.run_notifications(up, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats["sent"] == 1 and sent_to(up) == [402]
        ach._next_try.clear()  # the backoff is over
        stats = await ach.run_notifications(up, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats["sent"] == 1 and sent_to(up) == [401, 402]
        st = await state_of(sm, r1)
        assert st.sent_at is not None and st.last_error is None

    run_db(test)


def test_a_row_gives_up_after_max_attempts():
    async def test(sm):
        uid = await add_player(sm, 501)
        rid = await add_row(sm, uid, **GOLD)
        async with sm() as s, s.begin():
            await s.execute(text("UPDATE outbox SET attempts = :n WHERE id = :id"), {"n": ach.MAX_ATTEMPTS, "id": rid})
        bot = make_bot()
        stats = await ach.run_notifications(bot, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats["sent"] == 0 and stats["skipped"] == 0
        bot.send_message.assert_not_awaited()

    run_db(test)


def test_a_player_who_blocked_the_bot_is_opted_out():
    async def test(sm):
        uid = await add_player(sm, 601)
        rid = await add_row(sm, uid, **GOLD)
        bot = make_bot(fail=TelegramForbiddenError(MagicMock(), "Forbidden: bot was blocked by the user"))
        stats = await ach.run_notifications(bot, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats["blocked"] == 1 and stats["sent"] == 0
        st = await state_of(sm, rid)
        assert st.sent_at is not None and st.last_error == "not delivered: blocked the bot"
        (u,) = await fetch(sm, "SELECT notify_opt_in, notify_opt_out_at FROM users WHERE id = :id", id=uid)
        assert u.notify_opt_in is False and u.notify_opt_out_at is not None

    run_db(test)


def test_a_flood_wait_is_waited_out_once():
    async def test(sm):
        uid = await add_player(sm, 701)
        await add_row(sm, uid, **GOLD)
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        bot = make_bot(fail=[TelegramRetryAfter(MagicMock(), "Too Many Requests", 2), None])
        stats = await ach.run_notifications(bot, sm, make_settings(), now=NOW, sleep=fake_sleep)
        assert stats["sent"] == 1 and bot.send_message.await_count == 2 and sleeps[0] == 2.0

    run_db(test)


# ---------------------------------------------------------------- the job

def test_the_job_is_registered_and_idle_without_a_bot():
    spec = next(j for j in jobs.JOBS if j.id == "achievement_notify")
    assert spec.func is ach.achievement_notify and spec.trigger == "interval" and spec.trigger_args == {"seconds": 20}
    assert asyncio.run(ach.achievement_notify()) is None  # no runtime set up
    assert runtime.current() is None


def test_the_job_runs_through_the_runtime_and_never_raises():
    async def test(sm):
        uid = await add_player(sm, 801)
        rid = await add_row(sm, uid, **GOLD)
        stats = await ach.achievement_notify()
        assert stats is not None
        # the wall clock decides between "sent" and "quiet hours", either way the job answers and the row is consistent
        st = await state_of(sm, rid)
        assert (st.sent_at is not None) == (stats["sent"] == 1) and (stats["quiet"] == 1 or stats["sent"] == 1)

    run_db(test, bot=make_bot())
