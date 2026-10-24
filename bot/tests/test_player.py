"""S6 player side: reminder consent (/start toggle, callbacks, /stop), order reminders (selection, quiet hours, dedupe, exclusions,
throttling, failures) and the 5xx alert (math, cooldown, Redis keys). Aiogram objects are fakes; the database tests need the
throwaway Postgres (see tests/player_support.py) and are skipped without it."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandObject

from app import jobs
from app.handlers.start import on_start
from app.hr import runtime as hr_runtime
from app.player import alerts, keyboards, notify, reminders, runtime, store
from app.player.reminders import KIND_DEADLINE, KIND_ETA, due_reminders, is_quiet, reminder_text, run_reminders
from player_support import (add_player, fake_callback, fake_message, fake_user, fetch, make_bot, make_settings, order,
                                  run_db)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # 15:00 MSK
NOW_MS = NOW.timestamp() * 1000


def run(coro):
    return asyncio.run(coro)


def kinds(orders, now_ms=NOW_MS):
    return [(d.order_id, d.kind) for d in due_reminders(orders, now_ms)]


# ---------------------------------------------------------------- selection logic (pure)

def test_eta_window_is_15_minutes_and_future_only():
    o = lambda m: [order("a", now_ms=NOW_MS, eta_min=m, deadline_min=300)]  # noqa: E731
    assert kinds(o(10)) == [("a", KIND_ETA)]
    assert kinds(o(15)) == [("a", KIND_ETA)]
    assert kinds(o(15.1)) == []  # not yet
    assert kinds(o(0)) == [] and kinds(o(-5)) == []  # already past: a stale snapshot, the order is gone
    assert kinds([order("a", now_ms=NOW_MS, eta_min=5, eta_field="completesAt")]) == [("a", KIND_ETA)]  # alt field name


def test_deadline_window_is_60_minutes_and_future_only():
    o = lambda m: [order("a", now_ms=NOW_MS, deadline_min=m)]  # noqa: E731
    assert kinds(o(59)) == [("a", KIND_DEADLINE)]
    assert kinds(o(60)) == [("a", KIND_DEADLINE)]
    assert kinds(o(61)) == []
    assert kinds(o(0)) == [] and kinds(o(-1)) == []
    due = due_reminders(o(0.2), NOW_MS)
    assert due[0].minutes == 1  # never "0 min"


def test_eta_and_deadline_interplay():
    # it will finish in time and soon: only the eta reminder, never both
    assert kinds([order("a", now_ms=NOW_MS, eta_min=10, deadline_min=30)]) == [("a", KIND_ETA)]
    # eta later than the deadline: the order will not make it -> the burning-deadline reminder
    assert kinds([order("a", now_ms=NOW_MS, eta_min=40, deadline_min=30)]) == [("a", KIND_DEADLINE)]
    # finishes in time, but not soon: nothing to remind about even though the deadline is within the hour
    assert kinds([order("a", now_ms=NOW_MS, eta_min=40, deadline_min=50)]) == []
    # no eta known (before the S1 merge): deadline only
    assert kinds([order("a", now_ms=NOW_MS, deadline_min=30)]) == [("a", KIND_DEADLINE)]
    # eta only (no deadline): eta reminder
    assert kinds([order("a", now_ms=NOW_MS, eta_min=3)]) == [("a", KIND_ETA)]


def test_only_active_non_tutorial_orders_count():
    for status in ("available", "done", "failed", "cancelled", None):
        assert kinds([order("a", now_ms=NOW_MS, deadline_min=20, status=status)]) == [], status
    assert kinds([order("a", now_ms=NOW_MS, deadline_min=20, status="accepted")]) == [("a", KIND_DEADLINE)]
    assert kinds([order("a", now_ms=NOW_MS, deadline_min=20, tutorial=True)]) == []


def test_missing_or_malformed_fields_are_skipped_without_crashing():
    good = order("good", now_ms=NOW_MS, deadline_min=10)
    junk = [None, 5, "x", [], {}, {"status": "loading"}, {"id": 7, "status": "loading", "deadline": NOW_MS + 60_000},
            {"id": "", "status": "loading", "deadline": NOW_MS + 60_000},
            {"id": "n1", "status": "loading"}, {"id": "n2", "status": "loading", "deadline": None},
            {"id": "n3", "status": "loading", "deadline": "soon"}, {"id": "n4", "status": "loading", "deadline": True},
            {"id": "n5", "status": "loading", "deadline": float("nan")}, {"id": "n6", "status": "loading", "deadline": 1e99},
            {"id": "n7", "status": "loading", "deadline": 0}, {"id": "n8", "status": "loading", "etaAt": "x", "deadline": -5},
            {"id": "n9", "status": "loading", "deadline": NOW_MS + 60_000, "client": 42, "loaded": "many", "qty": None}]
    assert kinds(junk + [good]) == [("good", KIND_DEADLINE), ("n9", KIND_DEADLINE)] or kinds(junk + [good]) == [
        ("n9", KIND_DEADLINE), ("good", KIND_DEADLINE)]
    n9 = [d for d in due_reminders(junk, NOW_MS) if d.order_id == "n9"][0]
    assert (n9.client, n9.loaded, n9.qty) == ("без названия", None, None)
    for bad in (None, {}, "orders", 5, [], ()):
        assert due_reminders(bad, NOW_MS) == []


def test_results_are_sorted_by_urgency():
    orders = [order("late", now_ms=NOW_MS, deadline_min=50), order("soon", now_ms=NOW_MS, deadline_min=5),
              order("mid", now_ms=NOW_MS, eta_min=9)]
    assert [d.order_id for d in due_reminders(orders, NOW_MS)] == ["soon", "mid", "late"]


def test_quiet_hours_are_23_to_09_moscow():
    def at(h, m=0):  # MSK wall clock -> aware datetime in UTC
        return datetime(2026, 10, 24, h, m, tzinfo=timezone(timedelta(hours=3))).astimezone(timezone.utc)

    assert not is_quiet(at(9, 0)) and not is_quiet(at(15)) and not is_quiet(at(22, 59))
    assert is_quiet(at(23, 0)) and is_quiet(at(0, 30)) and is_quiet(at(8, 59))
    assert is_quiet(datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc))  # 23:00 MSK
    assert not is_quiet(datetime(2026, 10, 24, 19, 59, tzinfo=timezone.utc))  # 22:59 MSK


def test_reminder_text_escapes_and_caps_lines():
    due = due_reminders([order("a", now_ms=NOW_MS, eta_min=7, client="<b>Cafe</b> & Co"),
                         order("b", now_ms=NOW_MS, deadline_min=30)], NOW_MS)
    text = reminder_text(due)
    assert "&lt;b&gt;Cafe&lt;/b&gt; &amp; Co" in text and "<b>Cafe" not in text
    assert "~7 мин" in text and "(4/10)" in text and "/stop" in text
    many = due_reminders([order(f"o{i}", now_ms=NOW_MS, deadline_min=10 + i) for i in range(8)], NOW_MS)
    body = reminder_text(many)
    assert body.count("⏰") == reminders.MAX_LINES and "ещё заказов: 3" in body


# ---------------------------------------------------------------- consent: /start toggle, callbacks, /stop (database)

def test_start_creates_the_user_row_and_offers_the_toggle():
    async def test(sm):
        msg = fake_message(fake_user(777, first_name="Anna"))
        await on_start(msg, CommandObject(command="start", args="qr_hall3"), make_settings())
        kb = msg.answer.await_args.kwargs["reply_markup"]
        assert kb.inline_keyboard[0][0].web_app.url == "https://game.example.com?startapp=qr_hall3"
        toggle = kb.inline_keyboard[1][0]
        assert (toggle.text, toggle.callback_data) == (keyboards.NOTIFY_ON_TEXT, "notify:on")
        row = (await fetch(sm, "SELECT first_name, source, bot_started_at, notify_opt_in, notify_opt_in_at FROM users WHERE tg_id = 777"))[0]
        assert (row.first_name, row.source, row.notify_opt_in, row.notify_opt_in_at) == ("Anna", "qr_hall3", False, None)
        assert row.bot_started_at is not None
        assert await fetch(sm, "SELECT 1 FROM factories") == []  # no factory: the player never opened the app

    run_db(test)


def test_start_keeps_existing_data_and_shows_the_current_state():
    async def test(sm):
        uid = await add_player(sm, 778, opt_in=True, started=False)
        await fetch(sm, "SELECT 1")
        async with sm() as s, s.begin():
            await s.execute(store.text("UPDATE users SET source = 'ref_1' WHERE id = :u"), {"u": uid})
        msg = fake_message(fake_user(778))
        await on_start(msg, CommandObject(command="start", args="other"), make_settings())
        kb = msg.answer.await_args.kwargs["reply_markup"]
        assert kb.inline_keyboard[1][0].callback_data == "notify:off"  # already opted in
        row = (await fetch(sm, "SELECT source, bot_started_at, notify_opt_in FROM users WHERE tg_id = 778"))[0]
        assert row.source == "ref_1" and row.bot_started_at is not None and row.notify_opt_in is True  # first touch wins
        first = row.bot_started_at
        await on_start(fake_message(fake_user(778)), CommandObject(command="start", args=None), make_settings())
        assert (await fetch(sm, "SELECT bot_started_at FROM users WHERE tg_id = 778"))[0].bot_started_at == first

    run_db(test)


def test_start_survives_database_trouble_and_missing_database():
    async def broken(sm):
        original = store.register_start
        store.register_start = AsyncMock(side_effect=RuntimeError("db down"))
        try:
            msg = fake_message(fake_user(5))
            await on_start(msg, CommandObject(command="start", args=None), make_settings())
            kb = msg.answer.await_args.kwargs["reply_markup"]
            assert len(kb.inline_keyboard) == 1 and kb.inline_keyboard[0][0].web_app  # Play only, no toggle
        finally:
            store.register_start = original

    run_db(broken)
    msg = fake_message(fake_user(6))  # no runtime at all (DATABASE_URL empty)
    run(on_start(msg, CommandObject(command="start", args=None), make_settings(database_url="")))
    assert len(msg.answer.await_args.kwargs["reply_markup"].inline_keyboard) == 1
    msg = fake_message(fake_user(6))  # no https url and no database: plain text, no keyboard
    run(on_start(msg, CommandObject(command="start", args=None), make_settings(database_url="", webapp_url="")))
    assert "reply_markup" not in msg.answer.await_args.kwargs


def test_toggle_callbacks_opt_in_and_out():
    async def test(sm):
        user = fake_user(900)
        await store.register_start(sm, store.Profile(900, "ivan", "Ivan"))
        play_markup = keyboards.build_start_keyboard("https://game.example.com", False)

        cb = fake_callback("notify:on", user, play_markup)
        await notify.on_toggle(cb)
        row = (await fetch(sm, "SELECT notify_opt_in, notify_opt_in_at, notify_opt_out_at FROM users WHERE tg_id = 900"))[0]
        assert row.notify_opt_in is True and row.notify_opt_in_at is not None and row.notify_opt_out_at is None
        cb.answer.assert_awaited_once()
        new_markup = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
        assert new_markup.inline_keyboard[0][0].web_app is not None  # the Play row is kept
        assert [r[0].callback_data for r in new_markup.inline_keyboard[1:]] == ["notify:off"]
        assert "включены" in cb.message.answer.await_args.args[0]
        opted_at = row.notify_opt_in_at

        again = fake_callback("notify:on", user, new_markup)  # repeated tap: nothing changes, the keyboard stays
        await notify.on_toggle(again)
        again.message.edit_reply_markup.assert_not_awaited()
        assert (await fetch(sm, "SELECT notify_opt_in_at FROM users WHERE tg_id = 900"))[0].notify_opt_in_at == opted_at

        off = fake_callback("notify:off", user, new_markup)
        await notify.on_toggle(off)
        row = (await fetch(sm, "SELECT notify_opt_in, notify_opt_out_at FROM users WHERE tg_id = 900"))[0]
        assert row.notify_opt_in is False and row.notify_opt_out_at is not None
        assert off.message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard[-1][0].callback_data == "notify:on"

    run_db(test)


def test_stop_and_notify_commands():
    async def test(sm):
        msg = fake_message(fake_user(910))
        await notify.cmd_stop(msg)  # never opted in, no row: nothing to switch off, nothing created
        assert msg.answer.await_args.args[0] == notify.ALREADY_OFF_TEXT
        assert await fetch(sm, "SELECT 1 FROM users WHERE tg_id = 910") == []

        msg = fake_message(fake_user(910))
        await notify.cmd_notify(msg)  # opting in before ever starting the app creates the row
        assert msg.answer.await_args.args[0] == notify.ON_TEXT
        row = (await fetch(sm, "SELECT notify_opt_in, bot_started_at FROM users WHERE tg_id = 910"))[0]
        assert row.notify_opt_in is True and row.bot_started_at is not None

        msg = fake_message(fake_user(910))
        await notify.cmd_notify(msg)
        assert msg.answer.await_args.args[0] == notify.ALREADY_ON_TEXT
        msg = fake_message(fake_user(910))
        await notify.cmd_stop(msg)
        assert msg.answer.await_args.args[0] == notify.OFF_TEXT
        assert (await fetch(sm, "SELECT notify_opt_in, notify_opt_out_at FROM users WHERE tg_id = 910"))[0].notify_opt_in is False

    run_db(test)


def test_consent_handlers_without_a_database_answer_politely():
    hr_runtime._rt = None
    msg = fake_message()
    run(notify.cmd_stop(msg))
    assert msg.answer.await_args.args[0] == notify.UNAVAILABLE_TEXT
    cb = fake_callback("notify:on")
    run(notify.on_toggle(cb))
    cb.answer.assert_awaited_once()
    cb.message.edit_reply_markup.assert_not_awaited()


def test_notify_router_is_registered():
    from app.handlers import build_router

    names = {r.name for r in build_router().sub_routers}
    assert {"start", "notify"} <= names


# ---------------------------------------------------------------- reminders job (database)

def snap_orders(*orders):
    return list(orders)


def test_reminders_select_the_right_players_and_dedupe():
    async def test(sm):
        soon = [order("o1", now_ms=NOW_MS, eta_min=8, deadline_min=120)]
        await add_player(sm, 101, orders=soon)  # opted in, started
        await add_player(sm, 102, orders=soon, opt_in=False)  # never opted in / opted out
        await add_player(sm, 103, orders=soon, started=False)  # opted in but never started the bot (cannot be messaged)
        await add_player(sm, 104, orders=soon, is_bot=True)  # demo bot
        await add_player(sm, -1000001, orders=soon)  # negative id = seed bot
        await add_player(sm, 105, orders=[order("o2", now_ms=NOW_MS, deadline_min=500)])  # nothing close
        await add_player(sm, 106, orders=soon, factory=False)  # never opened the app: no snapshot
        await add_player(sm, 107, snapshot=None)  # factory without a snapshot yet
        await add_player(sm, 108, snapshot={"player": {}})  # snapshot without orders
        await add_player(sm, 109, snapshot={"orders": "broken"})
        await add_player(sm, 110, orders=[None, {"id": "x"}, order("o3", now_ms=NOW_MS, deadline_min=20)])  # junk + one good

        bot, sleeps = make_bot(), []

        async def fake_sleep(s):
            sleeps.append(s)

        stats = await run_reminders(bot, sm, make_settings(), now=NOW, sleep=fake_sleep)
        sent_to = sorted(c.args[0] for c in bot.send_message.await_args_list)
        assert sent_to == [101, 110]
        assert stats["sent"] == 2 and stats["failed"] == 0 and stats["skipped_dup"] == 0
        assert sleeps == [reminders.SEND_INTERVAL_S] * 2  # throttled: ~25 messages per second
        first = next(c for c in bot.send_message.await_args_list if c.args[0] == 101)
        assert "Магазин у дома" in first.args[1] and "~8 мин" in first.args[1]
        buttons = [b for row in first.kwargs["reply_markup"].inline_keyboard for b in row]
        assert {b.callback_data for b in buttons if b.callback_data} == {"notify:off"} and any(b.web_app for b in buttons)
        marks = await fetch(sm, "SELECT u.tg_id, r.order_id, r.kind FROM reminders_sent r JOIN users u ON u.id = r.user_id "
                                "ORDER BY u.tg_id")
        assert [(m.tg_id, m.order_id, m.kind) for m in marks] == [(101, "o1", "eta"), (110, "o3", "deadline")]

        bot.send_message.reset_mock()  # the next run: same orders, nothing new
        again = await run_reminders(bot, sm, make_settings(), now=NOW + timedelta(minutes=5), sleep=fake_sleep)
        bot.send_message.assert_not_awaited()
        assert again["sent"] == 0 and again["skipped_dup"] == 2

        await add_player(sm, 111, orders=soon + [order("o9", now_ms=NOW_MS, deadline_min=30)])  # one message, two orders
        await run_reminders(bot, sm, make_settings(), now=NOW + timedelta(minutes=5), sleep=fake_sleep)
        (call,) = bot.send_message.await_args_list
        assert call.args[0] == 111 and call.args[1].count("\n") >= 2 and "~3 мин" in call.args[1] and "~25 мин" in call.args[1]
        # a new order of an already reminded player is reminded, the old kind of the old order is not
        async with sm() as s, s.begin():
            await s.execute(store.text(
                "UPDATE factories SET snapshot = CAST(:s AS jsonb) WHERE user_id = (SELECT id FROM users WHERE tg_id = 101)"),
                {"s": __import__("json").dumps({"orders": soon + [order("o4", now_ms=NOW_MS, deadline_min=25)]})})
        bot.send_message.reset_mock()
        await run_reminders(bot, sm, make_settings(), now=NOW + timedelta(minutes=6), sleep=fake_sleep)
        (call,) = bot.send_message.await_args_list
        assert call.args[0] == 101 and "o4" not in call.args[1] and "осталось ~19 мин" in call.args[1]

    run_db(test)


def test_reminders_respect_quiet_hours_and_catch_up_later():
    async def test(sm):
        night = datetime(2026, 10, 24, 20, 30, tzinfo=timezone.utc)  # 23:30 MSK
        night_ms = night.timestamp() * 1000
        await add_player(sm, 201, orders=[order("n1", now_ms=night_ms, deadline_min=45)])
        bot = make_bot()
        stats = await run_reminders(bot, sm, make_settings(), now=night, sleep=AsyncMock())
        assert stats["quiet"] == 1 and stats["sent"] == 0
        bot.send_message.assert_not_awaited()
        assert await fetch(sm, "SELECT 1 FROM reminders_sent") == []  # nothing was claimed: it is retried when the night is over
        morning = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)  # 09:00 MSK
        await add_player(sm, 202, orders=[order("m1", now_ms=morning.timestamp() * 1000, deadline_min=30)])
        await run_reminders(bot, sm, make_settings(), now=morning, sleep=AsyncMock())
        assert [c.args[0] for c in bot.send_message.await_args_list] == [202]  # 201's deadline passed during the night

    run_db(test)


def test_failed_sends_release_the_claim_and_blocked_users_are_opted_out():
    async def test(sm):
        soon = [order("o1", now_ms=NOW_MS, eta_min=8)]
        await add_player(sm, 301, orders=soon)
        method = MagicMock()
        bot = make_bot(fail=TelegramAPIError(method, "boom"))
        stats = await run_reminders(bot, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats["failed"] == 1 and stats["sent"] == 0
        assert await fetch(sm, "SELECT 1 FROM reminders_sent") == []  # released: the next run tries again
        ok = make_bot()
        stats = await run_reminders(ok, sm, make_settings(), now=NOW + timedelta(minutes=5), sleep=AsyncMock())
        assert stats["sent"] == 1 and len(await fetch(sm, "SELECT 1 FROM reminders_sent")) == 1

        await add_player(sm, 302, orders=soon)
        blocked = make_bot(fail=TelegramForbiddenError(method, "Forbidden: bot was blocked by the user"))
        stats = await run_reminders(blocked, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert stats["blocked"] == 1
        row = (await fetch(sm, "SELECT notify_opt_in, notify_opt_out_at FROM users WHERE tg_id = 302"))[0]
        assert row.notify_opt_in is False and row.notify_opt_out_at is not None
        assert await fetch(sm, "SELECT 1 FROM reminders_sent r JOIN users u ON u.id = r.user_id WHERE u.tg_id = 302") == []

    run_db(test)


def test_flood_wait_waits_once_and_retries_and_one_failure_does_not_stop_the_run():
    async def test(sm):
        soon = [order("o1", now_ms=NOW_MS, eta_min=8)]
        for tg in (401, 402, 403):
            await add_player(sm, tg, orders=soon)
        flood = TelegramRetryAfter(MagicMock(), "Too Many Requests", 2)
        bot = make_bot(fail=[flood, None, TelegramAPIError(MagicMock(), "chat not found"), None])
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        stats = await run_reminders(bot, sm, make_settings(), now=NOW, sleep=fake_sleep)
        assert 2 in sleeps  # waited the requested time
        assert stats["sent"] == 2 and stats["failed"] == 1
        assert len(await fetch(sm, "SELECT 1 FROM reminders_sent")) == 2

    run_db(test)


def test_paging_covers_every_opted_in_player(monkeypatch):
    monkeypatch.setattr(reminders, "PAGE", 2)

    async def test(sm):
        for tg in range(501, 508):
            await add_player(sm, tg, orders=[order("p", now_ms=NOW_MS, eta_min=5)])
        bot = make_bot()
        await run_reminders(bot, sm, make_settings(), now=NOW, sleep=AsyncMock())
        assert sorted(c.args[0] for c in bot.send_message.await_args_list) == list(range(501, 508))

    run_db(test)


def test_reminder_job_is_idle_without_bot_or_database():
    hr_runtime._rt = None
    assert run(reminders.order_reminders()) is None  # runtime not set up
    hr_runtime.setup(make_settings(database_url=""), None, None)
    try:
        assert run(reminders.order_reminders()) is None  # no BOT_TOKEN
    finally:
        hr_runtime._rt = None
    hr_runtime.setup(make_settings(database_url=""), make_bot(), None)
    try:
        assert run(reminders.order_reminders()) is None  # no database
    finally:
        hr_runtime._rt = None


# ---------------------------------------------------------------- 5xx alert

def test_storm_math():
    assert not alerts.is_storm(19, 19)  # fewer than 20 requests: never an alert
    assert alerts.is_storm(20, 5) and not alerts.is_storm(20, 4)  # strictly more than 20 %
    assert not alerts.is_storm(100, 20) and alerts.is_storm(100, 21)
    assert not alerts.is_storm(0, 0) and not alerts.is_storm(1000, 0) and alerts.is_storm(20, 20)


def test_window_stamps_are_utc_minutes_across_boundaries():
    stamps = alerts.window_stamps(datetime(2026, 1, 1, 0, 3, tzinfo=timezone.utc))
    assert stamps[0] == "202601010003" and stamps[3] == "202601010000" and stamps[4] == "202512312359"
    assert len(stamps) == len(set(stamps)) == 10
    assert alerts.window_stamps(datetime(2026, 10, 24, 15, 0, tzinfo=timezone(timedelta(hours=3))))[0] == "202610241200"


class FakeRedis:
    def __init__(self, data=None):
        self.data = dict(data or {})
        self.asked = []

    async def mget(self, keys):
        self.asked.append(list(keys))
        return [self.data.get(k) for k in keys]


def counters(now, req, err, minute=0):
    stamp = alerts.window_stamps(now, max(10, minute + 1))[minute]
    return {alerts.REQ_PREFIX + stamp: str(req), alerts.ERR_PREFIX + stamp: str(err)}


def test_read_window_sums_ten_minutes_and_ignores_garbage():
    data = {}
    for minute, (req, err) in enumerate([(5, 1), (7, 2), (3, 0)]):
        data.update(counters(NOW, req, err, minute))
    data.update(counters(NOW, 100, 100, 10))  # 11 minutes ago: outside the window
    data[alerts.REQ_PREFIX + alerts.window_stamps(NOW)[5]] = "oops"
    redis = FakeRedis(data)
    assert run(alerts.read_window(redis, NOW)) == (15, 3)
    assert len(redis.asked[0]) == 20


def test_alert_goes_to_every_admin_with_numbers_then_cools_down():
    clock = SimpleNamespace(t=1000.0)
    state = alerts.AlertState(lambda: clock.t)
    redis, bot = FakeRedis(counters(NOW, 50, 20)), make_bot()
    result = run(alerts.check_5xx(bot, redis, {1002, 1001}, now=NOW, state=state))
    assert result["alerted"] and result["sent_to"] == 2 and (result["total"], result["errors"]) == (50, 20)
    assert [c.args[0] for c in bot.send_message.await_args_list] == [1001, 1002]
    text = bot.send_message.await_args_list[0].args[1]
    assert "20 из 50" in text and "40%" in text and "10 мин" in text

    bot.send_message.reset_mock()
    clock.t += alerts.COOLDOWN_S - 1  # still cooling down
    assert not run(alerts.check_5xx(bot, redis, {1001}, now=NOW, state=state))["alerted"]
    bot.send_message.assert_not_awaited()
    clock.t += 2  # cooldown over
    assert run(alerts.check_5xx(bot, redis, {1001}, now=NOW, state=state))["alerted"]
    assert bot.send_message.await_count == 1


def test_no_alert_below_threshold_or_when_recovered():
    state = alerts.AlertState()
    for req, err in ((19, 19), (100, 20), (100, 0), (0, 0)):
        bot = make_bot()
        res = run(alerts.check_5xx(bot, FakeRedis(counters(NOW, req, err)), {1001}, now=NOW, state=state))
        assert not res["alerted"] and not res["storm"], (req, err)
        bot.send_message.assert_not_awaited()


def test_failed_delivery_does_not_start_the_cooldown_and_partial_delivery_does():
    state = alerts.AlertState()
    redis = FakeRedis(counters(NOW, 40, 30))
    down = make_bot(fail=TelegramAPIError(MagicMock(), "telegram down"))
    res = run(alerts.check_5xx(down, redis, {1001, 1002}, now=NOW, state=state))
    assert not res["alerted"] and res["sent_to"] == 0 and not state.cooling_down()
    partial = make_bot(fail=[TelegramForbiddenError(MagicMock(), "blocked"), None])
    res = run(alerts.check_5xx(partial, redis, {1001, 1002}, now=NOW, state=state))
    assert res["alerted"] and res["sent_to"] == 1 and state.cooling_down()


def test_storm_without_admins_only_warns_once_per_cooldown(caplog):
    state = alerts.AlertState()
    redis, bot = FakeRedis(counters(NOW, 40, 30)), make_bot()
    caplog.set_level("WARNING")
    for _ in range(3):
        res = run(alerts.check_5xx(bot, redis, set(), now=NOW, state=state))
        assert not res["alerted"]
    assert caplog.text.count("HR_ADMIN_TG_IDS is empty") == 1
    bot.send_message.assert_not_awaited()


def test_alert_job_is_idle_without_bot_or_redis_and_never_raises(monkeypatch):
    hr_runtime._rt = None
    assert run(alerts.alert_5xx()) is None
    hr_runtime.setup(make_settings(redis_url="redis://x/0"), None, None)
    try:
        assert run(alerts.alert_5xx()) is None  # no BOT_TOKEN: does nothing
    finally:
        hr_runtime._rt = None
    hr_runtime.setup(make_settings(redis_url=""), make_bot(), None)
    try:
        assert run(alerts.alert_5xx()) is None  # no Redis url
        runtime.set_redis(FakeRedis(), key="redis://x/0")
        hr_runtime.setup(make_settings(redis_url="redis://x/0"), make_bot(), None)
        boom = AsyncMock(side_effect=ConnectionError("redis restarting"))
        monkeypatch.setattr(runtime.current().bot, "send_message", boom, raising=False)
        monkeypatch.setattr(alerts, "read_window", AsyncMock(side_effect=ConnectionError("redis restarting")))
        assert run(alerts.alert_5xx()) is None  # swallowed
    finally:
        hr_runtime._rt = None
        runtime.set_redis(None, key=None)


def test_alert_reads_the_keys_the_api_writes():
    """Real Redis, keys written the way api/app/metrics.py writes them (INCR metrics:req:{UTC minute} / metrics:5xx:{...})."""
    import redis.asyncio as aioredis

    url = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56380/0")
    if url.endswith("/0"):
        url = url[:-2] + "/13"  # a dedicated db index of the throwaway redis

    async def go():
        r = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=1)
        try:
            await r.flushdb()
        except Exception:
            pytest.skip("Redis not reachable")
        try:
            now = datetime.now(timezone.utc)
            this, prev = alerts.window_stamps(now)[:2]
            for _ in range(15):
                await r.incr(f"metrics:req:{this}")
            for _ in range(10):
                await r.incr(f"metrics:req:{prev}")
            for _ in range(7):
                await r.incr(f"metrics:5xx:{prev}")
            return await alerts.read_window(r, now)
        finally:
            await r.flushdb()
            await r.aclose()

    assert run(go()) == (25, 7)


def test_new_jobs_are_registered_with_sane_intervals():
    by_id = {j.id: j for j in jobs.JOBS}
    assert by_id["order_reminders"].trigger_args["minutes"] == 5
    assert by_id["alert_5xx"].trigger_args["minutes"] == 1
    assert by_id["order_reminders"].func is reminders.order_reminders and by_id["alert_5xx"].func is alerts.alert_5xx
