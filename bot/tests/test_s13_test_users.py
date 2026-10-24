"""S13: users.is_test (e2e / test players) are excluded from every HR number, the CSV, /top, order reminders and the
lead-card delivery -- exactly like demo bots. Method: take a snapshot of every query on the seeded dataset, add a
test user with a factory / lead / events / shifts / quiz answers, and require the snapshot to be unchanged."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import text

from app.hr import outbox, queries, runtime
from app.player import store
from app.hr.format import QUIZ_QUESTION_IDS
from hr_support import (NOW, add_events, add_factory, add_lead, add_quiz, add_shift, add_user,
                        make_bot, run_db, x)

QUIZ_TOTAL = len(QUIZ_QUESTION_IDS)


async def snapshot(sm) -> dict:
    async with sm() as s:
        season = await queries.current_season(s)
        leads, total = await queries.list_leads(s, queries.LeadFilter(include_revoked=True), 0, 100)
        return {
            "funnel": await queries.funnel(s, season),
            "sources": await queries.sources(s),
            "stats": await queries.stats(s, QUIZ_TOTAL),
            "leads": ([r["id"] for r in leads], total),
            "export": [r["id"] for r in await queries.export_rows(s, queries.LeadFilter())],
            "top": await queries.top(s, season["id"], None, True, 50),
            "top_real": await queries.top(s, season["id"], None, False, 50),
            "top_uni": await queries.top(s, season["id"], "МГУПП", True, 50),
        }


async def add_test_player(sm, *, flag_user: bool = True) -> int:
    async with sm() as s, s.begin():
        uid = await add_user(s, 9001, "e2e", source="ref_1", username="e2e_user")
        if flag_user:
            await x(s, "UPDATE users SET is_test = true WHERE id = :u", u=uid)
        await add_factory(s, uid, 2, "e2e · Мой цех", 99999, "МГУПП")
        await x(s, "UPDATE factories SET is_test = true WHERE user_id = :u", u=uid)
        await add_lead(s, uid, 2, "Тестовый Кандидат", "МГУПП", tags=["senior"])
        await add_events(s, uid, ["app_open", "game_start", "lead_submit"], ts=NOW)
        await add_shift(s, uid, 2, created=NOW)
        await add_quiz(s, uid, 3, QUIZ_TOTAL - 3)
    return uid


def test_test_users_are_invisible_to_every_hr_query():
    async def go(sm):
        before = await snapshot(sm)
        await add_test_player(sm)
        after = await snapshot(sm)
        assert after == before
        assert all(r["user_id"] != 0 and "e2e" not in r["name"] for r in after["top"])
    run_db(go)


def test_a_test_user_without_the_factory_flag_is_still_hidden_from_top():
    """/top joins users: users.is_test alone is enough."""
    async def go(sm):
        before = await snapshot(sm)
        uid = await add_test_player(sm)
        async with sm() as s, s.begin():
            await x(s, "UPDATE factories SET is_test = false WHERE user_id = :u", u=uid)
        assert await snapshot(sm) == before
    run_db(go)


def test_a_real_user_with_the_same_data_does_show_up():
    """The control: the same rows without the flag change the numbers, so the test above really tests the filter."""
    async def go(sm):
        before = await snapshot(sm)
        await add_test_player(sm, flag_user=False)
        after = await snapshot(sm)
        assert after != before and after["stats"]["users"] == before["stats"]["users"] + 1
    run_db(go)


def test_reminders_skip_test_users():
    async def go(sm):
        before = await store.opted_in_page(sm)
        async with sm() as s, s.begin():
            uid = await add_user(s, 9002, "e2e")
            await x(s, "UPDATE users SET is_test = true, notify_opt_in = true, bot_started_at = now() WHERE id = :u", u=uid)
            await add_factory(s, uid, 2, "f", 1, None)
            await x(s, "UPDATE factories SET snapshot = CAST(:s AS jsonb) WHERE user_id = :u", u=uid, s=json.dumps({"orders": []}))
        assert await store.opted_in_page(sm) == before
    run_db(go)


def test_outbox_rows_of_test_users_are_marked_sent_and_never_delivered():
    bot = make_bot()

    async def go(sm):
        uid = await add_test_player(sm)
        async with sm() as s, s.begin():
            lid = await x(s, "SELECT id FROM leads WHERE user_id = :u", u=uid)
            await x(s, "INSERT INTO outbox (kind, payload) VALUES ('lead_created', CAST(:p AS jsonb))",
                    p=json.dumps({"lead_id": lid, "user_id": uid, "tg_id": 9001, "season_id": 2, "full_name": "T"}))
            real = await add_user(s, 9003, "Анна Р")
            rlid = await add_lead(s, real, 2, "Реальный", "РХТУ")
            await x(s, "INSERT INTO outbox (kind, payload) VALUES ('lead_created', CAST(:p AS jsonb))",
                    p=json.dumps({"lead_id": rlid, "user_id": real, "tg_id": 9003, "season_id": 2, "full_name": "R"}))
        res = await outbox.deliver_once(runtime.get())
        assert res.test_skipped == 1 and res.sent == 1 and res.failed == 0
        texts = [c.args[1] for c in bot.send_message.call_args_list]
        assert texts and all("Тестовый" not in t for t in texts) and any("Реальный" in t for t in texts)
        async with sm() as s:
            rows = (await s.execute(text("SELECT sent_at IS NOT NULL AS sent, last_error FROM outbox ORDER BY id"))).all()
        assert [r.sent for r in rows] == [True, True] and rows[0].last_error == "test user: not delivered"
    run_db(go, bot=bot)
