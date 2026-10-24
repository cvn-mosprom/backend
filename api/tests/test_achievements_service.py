"""C1: achievements service on a real Postgres (skipped when absent): the hook inside the game transaction, idempotency, never
revoked, bots / test users skipped, the SQL-fed rules (shifts, quiz, seasons, referrals) at their boundaries, the recompute in
batches (+ POST /internal/achievements/recompute) and the backfill. In-process fake sim (tests/fake_sim.py)."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import event

from app import quiz_bank
from app.achievements import backfill, service
from app.achievements import catalog as cat
from app.game import CommandBody, GameService
from app.models import User
from tests.game_helpers import bootstrap, command, factory_row, key, place, shift
from tests.helpers import INTERNAL_TOKEN, TEST_DB_URL, TEST_REDIS_URL, dev

SKUS5 = {k: 1_000 + i for i, k in enumerate(cat.SKU_IDS[:5])}
TUTORIAL = ["q_intro", "q_brew", "q_bottles", "q_filler", "q_line", "q_dock"]
# first_line 10 + first_order 10 + bottles_1k 10 + sku_5 20 + no_defects 30
RICH = {"sim": {"produced": 1500, "qualitySum": 1500 * 0.99, "collection": SKUS5, "orders": []},
        "dialogs": {"done": TUTORIAL, "factsSeen": [], "lastHintAt": 0}}
RICH_IDS = {"first_line", "first_order", "bottles_1k", "sku_5", "no_defects"}
RICH_POINTS = 80


@pytest_asyncio.fixture
async def c(make_client, clean_db, clean_redis, fake_sim):
    client = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                               rate_limit_enabled=False, internal_token=INTERNAL_TOKEN)
    client.db, client.redis, client.sim = clean_db, clean_redis, fake_sim  # type: ignore[attr-defined]
    client.sm = client.app.state.sessionmaker  # type: ignore[attr-defined]
    return client


async def mk(c, tg: int, name: str | None = None, state: dict | None = None) -> int:
    """A player created through bootstrap; `state` is merged into factories.state (top-level keys)."""
    await bootstrap(c, tg, name)
    uid = await c.db.fetchval("SELECT id FROM users WHERE tg_id = $1", tg)
    if state:
        await c.db.execute("UPDATE factories SET state = state || $1::jsonb WHERE user_id = $2", json.dumps(state), uid)
    return uid


async def earned(c, uid: int) -> list[tuple[str, int]]:
    rows = await c.db.fetch("SELECT achievement_id, season_key FROM achievements_earned WHERE user_id = $1 ORDER BY id", uid)
    return [(r["achievement_id"], r["season_key"]) for r in rows]


async def earned_ids(c, uid: int) -> set[str]:
    return {a for a, _ in await earned(c, uid)}


async def points(c, uid: int) -> int:
    return await c.db.fetchval("SELECT coalesce(max(achievement_points), 0) FROM player_scores WHERE user_id = $1", uid) or 0


async def season_id(c) -> int:
    return await c.db.fetchval("SELECT max(id) FROM seasons")


async def recompute(c, **kw) -> dict:
    return await service.recompute(c.sm, **{"force": True, **kw})


async def add_shifts(c, uid: int, n: int, *, at: str = "2026-10-05 12:00+00", sid: int | None = None, profession: str = "lab",
                     result: str = "win", score: float = 0.8, metrics: dict | None = None, step_days: int = 0) -> None:
    """n shift rows; row i is created at `at` + i * step_days days."""
    await c.db.execute(
        "INSERT INTO shifts (user_id, season_id, building_id, profession, game_id, result, score, duration_ms, metrics, created_at) "
        "SELECT $1, $2, 'b1', $3, 'g1', $4, $5, 5000, $6::jsonb, $7::timestamptz + (i * $8) * interval '1 day' "
        "FROM generate_series(0, $9 - 1) AS i",
        uid, sid or await season_id(c), profession, result, score, json.dumps(metrics or {}),
        datetime.fromisoformat(at.replace("+00", "+00:00")), step_days, n)


async def add_days(c, uid: int, days: int, per_day: int = 1, start: str = "2026-10-05") -> None:
    """`days` consecutive MSK days with `per_day` shifts each (created at noon UTC = 15:00 MSK of the same date)."""
    d0 = datetime.fromisoformat(start + "T12:00:00+00:00")
    for d in range(days):
        await add_shifts(c, uid, per_day, at=(d0 + timedelta(days=d)).isoformat())


# ---------------------------------------------------------------- the hook (a): inside the command

async def test_hook_unlocks_inside_the_command_and_the_http_body_is_unchanged(c):
    uid = await mk(c, 1, state=RICH)
    r = await command(c, 0, key(1), place())
    assert r.status_code == 200, r.text
    assert {"ok", "version", "result", "events", "snapshot", "replayed"} <= set(r.json())
    assert not any("chiev" in k for k in r.json())  # the HTTP body has no achievements key yet (C2 adds achievementsUnlocked)
    assert await earned_ids(c, uid) == RICH_IDS
    rows = await c.db.fetch("SELECT * FROM achievements_earned WHERE user_id = $1", uid)
    assert {r_["granted_via"] for r_ in rows} == {"hook"} and {r_["catalog_version"] for r_ in rows} == {cat.CATALOG_VERSION}
    ev = json.loads(next(r_ for r_ in rows if r_["achievement_id"] == "bottles_1k")["evidence"])
    assert ev == {"produced": 1500}
    sid = await season_id(c)
    assert all(r_["earned_season_id"] == sid and r_["season_key"] == 0 for r_ in rows)
    ps = await c.db.fetchrow("SELECT * FROM player_scores WHERE user_id = $1", uid)
    assert ps["achievement_points"] == ps["season_points"] == RICH_POINTS and ps["evaluated_at"] is None
    assert json.loads(ps["competency"]) == {} and json.loads(ps["fit"]) == {}  # C2 fills them
    prog = {r_["achievement_id"]: (r_["current"], r_["target"]) for r_ in await c.db.fetch(
        "SELECT * FROM achievement_progress WHERE user_id = $1 AND achievement_id <> '_counters'", uid)}
    assert prog["bottles_1k"] == (1000, 1000) and prog["bottles_10k"] == (1500, 10_000) and prog["sku_all"] == (5, 8)
    assert not any(k.startswith(("season_top", "quiz", "reaction")) for k in prog)  # not cheap rules: no progress rows yet


async def test_game_service_command_exposes_the_unlocked_list(c):
    uid = await mk(c, 1, state=RICH)
    svc = GameService(c.sm, c.redis, c.app.state.sim, bots_enabled=True)
    async with c.sm() as s:
        user = await s.get(User, uid)
    body = await svc.command(user, 0, key(2), place())
    assert isinstance(body, CommandBody) and [a["id"] for a in body.achievements_unlocked] == [
        "first_line", "first_order", "bottles_1k", "sku_5", "no_defects"]  # catalog order
    first = body.achievements_unlocked[0]
    assert set(first) == {"id", "title", "description", "category", "tier", "points", "hidden", "seasonal"} and first["points"] == 10
    json.dumps(dict(body)) and None  # still a plain dict on the wire
    again = await svc.command(user, 1, key(3), place(2, 2))
    assert again.achievements_unlocked == []  # nothing new the second time
    replay = await svc.command(user, 1, key(3), place(2, 2))  # idempotent replay
    assert replay["replayed"] is True and not getattr(replay, "achievements_unlocked", [])


async def test_rejected_commands_do_not_run_the_hook(c):
    uid = await mk(c, 1, state=RICH)
    r = await command(c, 0, key(1), {"type": "acceptOrder", "orderId": "x"})  # the fake sim rejects it
    assert r.status_code == 422
    assert await earned(c, uid) == [] and await c.db.fetchval("SELECT count(*) FROM player_scores") == 0


async def test_idempotent_and_points_stable(c):
    uid = await mk(c, 1, state=RICH)
    await command(c, 0, key(1), place())
    first = await c.db.fetch("SELECT id, achievement_id, earned_at, points FROM achievements_earned WHERE user_id = $1 ORDER BY id", uid)
    for v in (1, 2, 3):
        assert (await command(c, v, key(10 + v), place(v + 1, 1))).status_code == 200
    r1, r2 = await recompute(c), await recompute(c)
    assert r1["granted"] == r2["granted"] == 0 and r1["evaluated"] == 1
    again = await c.db.fetch("SELECT id, achievement_id, earned_at, points FROM achievements_earned WHERE user_id = $1 ORDER BY id", uid)
    assert [tuple(x) for x in again] == [tuple(x) for x in first]  # same rows, nothing re-granted
    assert await points(c, uid) == RICH_POINTS
    assert await c.db.fetchval("SELECT count(*) FROM player_scores WHERE user_id = $1", uid) == 1


async def test_two_concurrent_full_evaluations_never_double_grant(c):
    uid = await mk(c, 1, state=RICH)
    a, b = await asyncio.gather(recompute(c), recompute(c))
    assert a["granted"] + b["granted"] == len(RICH_IDS)
    assert sorted(await earned_ids(c, uid)) == sorted(RICH_IDS) and len(await earned(c, uid)) == len(RICH_IDS)
    assert await points(c, uid) == RICH_POINTS


async def test_never_revoked_when_the_state_regresses(c):
    uid = await mk(c, 1, state=RICH)
    await command(c, 0, key(1), place())
    before = await earned(c, uid)
    # a season reset / a lost state: nothing the engine sees supports the achievements any more
    await c.db.execute("UPDATE factories SET state = state || $1::jsonb WHERE user_id = $2", json.dumps({
        "sim": {"produced": 0, "qualitySum": 0, "collection": {}, "orders": []}, "dialogs": {"done": [], "factsSeen": []}}), uid)
    assert (await command(c, 1, key(2), place(2, 2))).status_code == 200
    await recompute(c)
    assert await earned(c, uid) == before and await points(c, uid) == RICH_POINTS
    prog = await c.db.fetchval("SELECT current FROM achievement_progress WHERE user_id = $1 AND achievement_id = 'bottles_10k'", uid)
    assert prog == 1500  # progress is monotonic too


async def test_bots_and_test_users_are_skipped(c):
    real = await mk(c, 1, state=RICH)
    tst = await mk(c, 2, "e2e-player", state=RICH)
    bot = await mk(c, 3, state=RICH)
    await c.db.execute("UPDATE users SET is_bot = true WHERE id = $1", bot)
    assert await c.db.fetchval("SELECT is_test FROM users WHERE id = $1", tst) is True
    for tg, v in ((1, 0), (2, 0), (3, 0)):
        assert (await command(c, v, key(tg), place(), tg=tg)).status_code == 200
    assert await earned_ids(c, real) == RICH_IDS and await earned(c, tst) == [] and await earned(c, bot) == []
    res = await recompute(c)
    assert res["evaluated"] == 1  # only the real player
    assert await earned(c, tst) == [] and await earned(c, bot) == []
    assert await c.db.fetchval("SELECT count(*) FROM player_scores WHERE user_id IN ($1, $2)", tst, bot) == 0
    assert await c.db.fetchval("SELECT count(*) FROM achievement_progress WHERE user_id IN ($1, $2)", tst, bot) == 0


async def test_hook_runs_in_the_commands_transaction_a_rollback_takes_it_back(c, monkeypatch):
    uid = await mk(c, 1, state=RICH)
    real = service.on_command
    seen = []

    async def hook_then_fail(*a, **kw):
        out = await real(*a, **kw)
        seen.append(out)
        raise RuntimeError("the transaction fails after the hook")

    monkeypatch.setattr(service, "on_command", hook_then_fail)
    before = await factory_row(c)
    r = await command(c, 0, key(1), place())
    assert r.status_code == 500 and seen and len(seen[0]) == len(RICH_IDS)  # the hook did unlock them ...
    assert await earned(c, uid) == [] and await c.db.fetchval("SELECT count(*) FROM player_scores") == 0  # ... and they rolled back
    assert await c.db.fetchval("SELECT count(*) FROM achievement_progress") == 0
    after = await factory_row(c)
    assert after["version"] == before["version"] == 0 and after["state"] == before["state"]
    monkeypatch.undo()
    assert (await command(c, 0, key(1), place())).status_code == 200  # the same command works afterwards
    assert await earned_ids(c, uid) == RICH_IDS


async def test_a_failing_hook_never_breaks_the_command(c, monkeypatch):
    uid = await mk(c, 1, state=RICH)

    async def broken(*a, **kw):
        raise RuntimeError("achievements are down")

    monkeypatch.setattr(service, "load_previous", broken)
    r = await command(c, 0, key(1), place())
    assert r.status_code == 200 and r.json()["version"] == 1
    assert (await factory_row(c))["version"] == 1 and await earned(c, uid) == []
    monkeypatch.undo()
    assert (await command(c, 1, key(2), place(3, 3))).status_code == 200  # the connection is not poisoned
    assert await earned_ids(c, uid) == RICH_IDS


async def test_a_database_error_inside_the_hook_is_rolled_back_to_its_savepoint(c, monkeypatch):
    """A failed statement poisons a Postgres transaction; the hook's savepoint keeps the command's own writes alive."""
    uid = await mk(c, 1, state=RICH)

    async def bad_sql(session, *a, **kw):
        await session.execute(sa.text("SELECT 1 / 0"))

    monkeypatch.setattr(service, "write_results", bad_sql)
    r = await command(c, 0, key(1), place())
    assert r.status_code == 200 and (await factory_row(c))["version"] == 1
    assert (await factory_row(c))["state"]["buildings"] and await earned(c, uid) == []  # the command's write survived
    monkeypatch.undo()
    assert (await command(c, 1, key(2), place(2, 2))).status_code == 200
    assert await earned_ids(c, uid) == RICH_IDS


async def test_cheap_hook_is_one_select_when_nothing_changes_and_fast(c):
    uid = await mk(c, 1, state=RICH)
    await command(c, 0, key(1), place())  # unlocks + writes the progress / counters rows
    statements: list[str] = []

    @event.listens_for(c.app.state.engine.sync_engine, "before_cursor_execute")
    def _log(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        statements.append(statement)

    state = (await factory_row(c))["state"]
    async with c.sm() as s, s.begin():
        user = await s.get(User, uid)
        statements.clear()
        out = await service.on_command(s, user, await season_id(c), state)
    work = [q for q in statements if not q.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO"))]
    assert out == [] and len(work) == 1 and "FOR UPDATE" in work[0], work
    n, t0 = 20, time.perf_counter()
    async with c.sm() as s, s.begin():
        user = await s.get(User, uid)
        for _ in range(n):
            await service.on_command(s, user, await season_id(c), state)
    per_call_ms = (time.perf_counter() - t0) / n * 1000
    assert per_call_ms < 25, f"hook took {per_call_ms:.1f} ms"  # ~2 ms locally; the bound only catches a regression to heavy queries


# ---------------------------------------------------------------- the hook (b): after an accepted submitShift

async def test_shift_hook_runs_shift_rules(c):
    uid = await mk(c, 1)
    r = await command(c, 0, key(1), shift(score=1.0, result="win"))
    assert r.status_code == 200
    assert await earned_ids(c, uid) == {"perfect_shift"}  # a hidden one, from the shifts table, in the shift's own transaction
    assert await c.db.fetchval("SELECT granted_via FROM achievements_earned WHERE user_id = $1", uid) == "shift"
    for i in range(1, 5):
        assert (await command(c, i, key(1 + i), shift())).status_code == 200
    assert "lab_practice" in await earned_ids(c, uid)  # 5 shifts of the lab profession, counted from the table (the fake sim keeps no skills)
    assert await c.db.fetchval("SELECT count(*) FROM shifts WHERE user_id = $1", uid) == 5
    assert await points(c, uid) == 20 + 10


async def test_a_non_shift_command_does_not_run_shift_rules(c):
    uid = await mk(c, 1)
    await add_shifts(c, uid, 12, metrics={"reaction_ms": 300})  # would be reaction_fast
    assert (await command(c, 0, key(1), place())).status_code == 200
    assert await earned(c, uid) == []
    assert (await recompute(c))["granted"] >= 1 and "reaction_fast" in await earned_ids(c, uid)


# ---------------------------------------------------------------- recompute: SQL-fed rules at their boundaries

async def test_reaction_boundary(c):
    uid = await mk(c, 1)
    await add_shifts(c, uid, 9, metrics={"reaction_ms": 399.9})
    await add_shifts(c, uid, 3, metrics={"reaction_ms": 400})  # not below
    await add_shifts(c, uid, 2, metrics={"reaction_ms": "fast"})  # not a number: ignored, no SQL error
    await add_shifts(c, uid, 2, metrics={"accuracy": 1})
    await recompute(c)
    assert "reaction_fast" not in await earned_ids(c, uid)
    await add_shifts(c, uid, 1, metrics={"reaction_ms": 100})
    await recompute(c)
    assert "reaction_fast" in await earned_ids(c, uid)


async def test_perfect_shift_boundary(c):
    uid = await mk(c, 1)
    await add_shifts(c, uid, 1, score=0.98)
    await add_shifts(c, uid, 1, score=1.0, result="fail")
    await recompute(c)
    assert "perfect_shift" not in await earned_ids(c, uid)
    await add_shifts(c, uid, 1, score=0.99)
    await recompute(c)
    assert "perfect_shift" in await earned_ids(c, uid)


@pytest.mark.parametrize("days, earned", [(6, False), (7, True)])
async def test_week_streak_from_the_table(c, days, earned):
    uid = await mk(c, 1)
    await add_days(c, uid, days)
    await recompute(c)
    assert ("week_streak" in await earned_ids(c, uid)) is earned


async def test_streak_uses_moscow_days(c):
    """21:30 UTC is 00:30 MSK of the NEXT date: seven nightly shifts on seven UTC dates are seven MSK days, and a shift at 20:59 UTC
    and one at 21:01 UTC of the same UTC date are two different MSK days."""
    uid = await mk(c, 1)
    await add_shifts(c, uid, 7, at="2026-10-05 21:30+00", step_days=1)
    agg = (await _agg(c, uid))["shifts"]
    assert [d for d, _ in agg["days"]] == [f"2026-10-0{n}" for n in range(6, 10)] + ["2026-10-10", "2026-10-11", "2026-10-12"]
    uid2 = await mk(c, 2)
    await add_shifts(c, uid2, 1, at="2026-10-05 20:59+00")
    await add_shifts(c, uid2, 1, at="2026-10-05 21:01+00")
    assert [d for d, _ in (await _agg(c, uid2))["shifts"]["days"]] == ["2026-10-05", "2026-10-06"]
    await recompute(c)
    assert "week_streak" in await earned_ids(c, uid) and "week_streak" not in await earned_ids(c, uid2)


async def _agg(c, uid: int) -> dict:
    async with c.sm() as s:
        return (await service.gather_aggregates(s, [uid]))[uid]


@pytest.mark.parametrize("full, earned", [(4, False), (5, True)])
async def test_full_days_from_the_table(c, full, earned):
    uid = await mk(c, 1)
    await add_days(c, uid, full, per_day=5, start="2026-10-01")
    await add_days(c, uid, 6, per_day=4, start="2026-11-01")  # four a day is not "all five"
    await recompute(c)
    assert ("full_days" in await earned_ids(c, uid)) is earned


async def test_quiz_boundaries_and_the_old_bank_is_ignored(c):
    uid = await mk(c, 1)
    ids = list(quiz_bank.IDS)

    async def answer(qid: str, correct: bool) -> None:
        await c.db.execute("INSERT INTO quiz_answers (user_id, question_id, correct, answer) VALUES ($1, $2, $3, 0)", uid, qid, correct)

    for q in ids[:5]:
        await answer(q, True)
    for q in ids[5:]:
        await answer(q, False)
    await answer("q1978", True)  # a question of the old bank: never counted
    await recompute(c)
    assert not {"quiz_6", "quiz_8"} & await earned_ids(c, uid)
    await c.db.execute("UPDATE quiz_answers SET correct = true WHERE user_id = $1 AND question_id = $2", uid, ids[5])
    await recompute(c)
    got = await earned_ids(c, uid)
    assert "quiz_6" in got and "quiz_8" not in got
    await c.db.execute("UPDATE quiz_answers SET correct = true WHERE user_id = $1", uid)
    await recompute(c)  # a quiz answer newer than the last evaluation makes the player due again
    assert "quiz_8" in await earned_ids(c, uid)


async def _mk_seasons(c, n: int) -> list[int]:
    """Seasons 2..n+1 (season 1 exists after bootstrap)."""
    out = []
    for i in range(n):
        out.append(await c.db.fetchval(
            "INSERT INTO seasons (starts_at, ends_at, days, closed) VALUES (now() - interval '30 days', now() - interval '20 days', 7, true) "
            "RETURNING id"))
    return out


@pytest.mark.parametrize("place_, expect", [(51, set()), (50, {"season_top50"}), (11, {"season_top50"}), (10, {"season_top50", "season_top10"}),
                                            (4, {"season_top50", "season_top10"}),
                                            (3, {"season_top50", "season_top10", "season_top3"})])
async def test_season_place_from_season_results(c, place_, expect):
    uid = await mk(c, 1)
    (sid,) = await _mk_seasons(c, 1)
    await c.db.execute("INSERT INTO season_results (season_id, user_id, place, stars, is_bot) VALUES ($1, $2, $3, 100, false)", sid, uid, place_)
    await recompute(c)
    assert set(await earned(c, uid)) == {(a, sid) for a in expect}  # seasonal: season_key = the season id
    assert await c.db.fetchval("SELECT (evidence->>'place')::int FROM achievements_earned WHERE user_id = $1 LIMIT 1", uid) in (place_, None)


async def test_season_achievements_repeat_per_season_and_count_every_time(c):
    uid = await mk(c, 1)
    s2, s3 = await _mk_seasons(c, 2)
    for sid, p in ((s2, 3), (s3, 40)):
        await c.db.execute("INSERT INTO season_results (season_id, user_id, place, stars, is_bot) VALUES ($1, $2, $3, 1, false)", sid, uid, p)
    await recompute(c)
    assert set(await earned(c, uid)) == {("season_top50", s2), ("season_top10", s2), ("season_top3", s2), ("season_top50", s3)}
    assert await points(c, uid) == 30 + 60 + 100 + 30
    await recompute(c)
    assert len(await earned(c, uid)) == 4


async def test_frozen_bot_rows_do_not_count(c):
    uid = await mk(c, 1)
    (sid,) = await _mk_seasons(c, 1)
    await c.db.execute("INSERT INTO season_results (season_id, user_id, place, stars, is_bot) VALUES ($1, $2, 1, 1, true)", sid, uid)
    await recompute(c)
    assert await earned(c, uid) == []


@pytest.mark.parametrize("n, expect", [(14, False), (15, True)])
async def test_season_regular_counts_shifts_of_one_season(c, n, expect):
    uid = await mk(c, 1)
    sid = await season_id(c)
    await add_shifts(c, uid, n, sid=sid)
    await recompute(c)
    assert (("season_regular", sid) in await earned(c, uid)) is expect
    if not expect:
        cur = await c.db.fetchval("SELECT current FROM achievement_progress WHERE user_id = $1 AND achievement_id = 'season_regular' "
                                  "AND season_key = $2", uid, sid)
        assert cur == 14


async def test_shifts_of_two_seasons_do_not_add_up_for_the_seasonal_rule(c):
    uid = await mk(c, 1)
    sid = await season_id(c)
    (other,) = await _mk_seasons(c, 1)
    await add_shifts(c, uid, 10, sid=sid)
    await add_shifts(c, uid, 10, sid=other)
    await recompute(c)
    assert not any(a == "season_regular" for a, _ in await earned(c, uid))


async def test_mentor_counts_invited_real_players(c):
    uid = await mk(c, 1)
    others = [await mk(c, 100 + i) for i in range(3)]
    for o in others[:2]:
        await c.db.execute("UPDATE users SET source = $1 WHERE id = $2", f"ref_{uid}", o)
    await recompute(c)
    assert "mentor" not in await earned_ids(c, uid)
    await c.db.execute("UPDATE users SET source = $1 WHERE id = $2", f"ref_{uid}", others[2])
    await c.db.execute("UPDATE users SET is_test = true WHERE id = $1", others[2])  # a test player does not count
    await c.db.execute("UPDATE factories SET updated_at = now() WHERE user_id = $1", uid)
    await recompute(c)
    assert "mentor" not in await earned_ids(c, uid)
    await c.db.execute("UPDATE users SET is_test = false WHERE id = $1", others[2])
    await recompute(c)
    assert "mentor" in await earned_ids(c, uid)


async def test_self_invite_does_not_count(c):
    uid = await mk(c, 1)
    await c.db.execute("UPDATE users SET source = $1 WHERE id = $2", f"ref_{uid}", uid)
    async with c.sm() as s:
        assert (await service.gather_aggregates(s, [uid]))[uid]["referrals"] == 0


async def test_profession_ace_from_the_table(c):
    uid = await mk(c, 1)
    await add_shifts(c, uid, 7, profession="operator", result="win", score=0.9)
    await add_shifts(c, uid, 3, profession="operator", result="fail", score=0.7)
    await recompute(c)  # 10 played, 7 wins (70%), avg (7 * 0.9 + 3 * 0.7) / 10 = 0.84 -> not yet
    got = await earned_ids(c, uid)
    assert "operator_practice" in got and "operator_ace" not in got
    await c.db.execute("UPDATE shifts SET score = 0.95 WHERE user_id = $1 AND result = 'win'", uid)  # avg 0.875
    await recompute(c)
    assert "operator_ace" in await earned_ids(c, uid)
    assert await c.db.fetchval("SELECT points FROM achievements_earned WHERE user_id = $1 AND achievement_id = 'operator_ace'", uid) == 40


# ---------------------------------------------------------------- recompute in batches, the internal endpoint, backfill

def post(c, body=None, token: str | None = INTERNAL_TOKEN):
    headers = {"X-Internal-Token": token} if token is not None else {}
    return c.post("/internal/achievements/recompute", json=body, headers=headers)


async def test_internal_endpoint_needs_the_token(c):
    for token in (None, "wrong", ""):
        r = await post(c, {}, token=token)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert (await post(c, {})).status_code == 200


async def test_internal_endpoint_validates_its_body(c):
    for body in ({"limit": 0}, {"limit": 1001}, {"limit": "x"}, {"after": -1}, {"since": "tomorrow"}):
        assert (await post(c, body)).status_code == 422, body


async def test_internal_endpoint_batches_and_resumes(c):
    uids = [await mk(c, 10 + i, state=RICH) for i in range(5)]
    await mk(c, 99, "e2e-t", state=RICH)  # a test player is never picked
    r1 = (await post(c, {"limit": 2})).json()
    assert (r1["evaluated"], r1["remaining"], r1["next_after"]) == (2, True, uids[1])
    assert r1["granted"] == 2 * len(RICH_IDS) and r1["points"] == 2 * RICH_POINTS and r1["unlocked"]["bottles_1k"] == 2
    r2 = (await post(c, {"limit": 2, "after": r1["next_after"]})).json()
    assert (r2["evaluated"], r2["remaining"], r2["next_after"]) == (2, True, uids[3])
    r3 = (await post(c, {"limit": 2, "after": r2["next_after"]})).json()
    assert (r3["evaluated"], r3["remaining"], r3["next_after"]) == (1, False, uids[4])
    assert {"evaluated", "failed", "granted", "points", "unlocked", "remaining", "next_after", "ms"} <= set(r3)
    for uid in uids:
        assert await earned_ids(c, uid) == RICH_IDS and await points(c, uid) == RICH_POINTS
    # up to date: a plain run (no force) has nothing to do; the same ids again grant nothing
    idle = (await post(c, {})).json()
    assert idle["evaluated"] == 0 and idle["remaining"] is False and idle["granted"] == 0
    forced = (await post(c, {"force": True})).json()
    assert forced["evaluated"] == 5 and forced["granted"] == 0


async def test_recompute_picks_up_changed_players_only(c):
    a, b = await mk(c, 1, state=RICH), await mk(c, 2, state=RICH)
    assert (await service.recompute(c.sm))["evaluated"] == 2
    assert (await service.recompute(c.sm))["evaluated"] == 0
    await command(c, 0, key(1), place(), tg=1)  # player 1 plays: factory written after the last evaluation
    assert (await service.recompute(c.sm))["evaluated"] == 1
    assert (await service.recompute(c.sm))["evaluated"] == 0
    await c.db.execute("INSERT INTO quiz_answers (user_id, question_id, correct, answer) VALUES ($1, $2, true, 0)", b, quiz_bank.IDS[0])
    assert (await service.recompute(c.sm))["evaluated"] == 1  # a quiz answer after the last evaluation
    assert (await service.recompute(c.sm, since=datetime.now(timezone.utc) + timedelta(days=1), force=True))["evaluated"] == 0
    assert (await service.recompute(c.sm, dry_run=True, force=True))["evaluated"] == 2


async def test_dry_run_writes_nothing(c):
    uid = await mk(c, 1, state=RICH)
    r = await service.recompute(c.sm, dry_run=True)
    assert r["evaluated"] == 1 and r["granted"] == len(RICH_IDS) and r["points"] == RICH_POINTS
    assert await earned(c, uid) == [] and await c.db.fetchval("SELECT count(*) FROM player_scores") == 0
    assert await c.db.fetchval("SELECT count(*) FROM achievement_progress") == 0


async def test_one_broken_player_does_not_stop_the_batch(c, monkeypatch):
    good, bad = await mk(c, 1, state=RICH), await mk(c, 2, state=RICH)
    real = service.compute

    def flaky(item, prev, *, cheap):
        if item.user_id == bad:
            raise ValueError("boom")
        return real(item, prev, cheap=cheap)

    monkeypatch.setattr(service, "compute", flaky)
    r = await service.recompute(c.sm)
    assert (r["evaluated"], r["failed"]) == (1, 1)
    assert await earned_ids(c, good) == RICH_IDS and await earned(c, bad) == []
    monkeypatch.undo()
    assert (await service.recompute(c.sm))["evaluated"] == 1  # the broken one was not marked evaluated: due again
    assert await earned_ids(c, bad) == RICH_IDS


async def test_backfill_grants_to_existing_players_once(c):
    ids = [await mk(c, 20 + i, state=RICH) for i in range(3)]
    await mk(c, 29, "e2e-x", state=RICH)
    bot = await mk(c, 28, state=RICH)
    await c.db.execute("UPDATE users SET is_bot = true WHERE id = $1", bot)
    await add_days(c, ids[0], 7)
    res = await backfill.run(c.sm, batch=2)
    assert res["users"] == 3 and res["granted"] == 3 * len(RICH_IDS) + 2 and res["failed"] == 0 and res["batches"] == 2
    assert res["unlocked"]["week_streak"] == res["unlocked"]["lab_practice"] == 1 and res["unlocked"]["first_line"] == 3
    assert res["points"] == 3 * RICH_POINTS + 30 + 10
    assert await c.db.fetchval("SELECT DISTINCT granted_via FROM achievements_earned WHERE user_id = $1 AND achievement_id = 'first_line'", ids[0]) == "backfill"
    again = await backfill.run(c.sm, batch=2)
    assert again["users"] == 3 and again["granted"] == 0 and again["points"] == 0
    assert await c.db.fetchval("SELECT count(*) FROM achievements_earned") == 3 * len(RICH_IDS) + 2
    dry = await backfill.run(c.sm, dry_run=True)
    assert dry["dry_run"] is True and dry["granted"] == 0


async def test_player_scores_follow_the_season_of_the_factory(c):
    uid = await mk(c, 1, state=RICH)
    await recompute(c)
    s1 = await season_id(c)
    (s2,) = await _mk_seasons(c, 1)
    await c.db.execute("UPDATE factories SET season_id = $1 WHERE user_id = $2", s2, uid)
    await recompute(c)
    rows = {r_["season_id"]: (r_["achievement_points"], r_["season_points"]) for r_ in await c.db.fetch(
        "SELECT * FROM player_scores WHERE user_id = $1", uid)}
    assert rows == {s1: (RICH_POINTS, RICH_POINTS), s2: (RICH_POINTS, 0)}  # lifetime points on both, the season part only where earned
    idx = await c.db.fetchval("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_player_scores_season_points'")
    assert idx == 1


async def test_counters_row_survives_and_orders_count_across_commands(c):
    uid = await mk(c, 1, state={"sim": {"produced": 0, "qualitySum": 0, "collection": {}, "orders": [
        {"id": f"o{i}", "status": "done", "minQuality": 0.9, "qty": 1, "loaded": 1} for i in range(6)]}})
    await command(c, 0, key(1), place())
    await c.db.execute("UPDATE factories SET state = jsonb_set(state, '{sim,orders}', $1::jsonb) WHERE user_id = $2", json.dumps(
        [{"id": f"p{i}", "status": "done", "minQuality": 0.9, "qty": 1, "loaded": 1} for i in range(4)]), uid)  # the first six were pruned
    await command(c, 1, key(2), place(2, 2))
    meta = json.loads(await c.db.fetchval("SELECT meta FROM achievement_progress WHERE user_id = $1 AND achievement_id = '_counters'", uid))
    assert meta["done"] == 10 and meta["otk"] == 10 and len(meta["seen"]) == 10
    assert {"on_time_10", "otk_control"} <= await earned_ids(c, uid)


async def test_big_batches_stay_under_the_bind_parameter_limit(c):
    """200 players x ~30 progress rows x 6 columns is more than asyncpg's 32767 parameters per statement: the writes are chunked."""
    await mk(c, 1)
    sid = await season_id(c)
    await c.db.execute("INSERT INTO users (tg_id) SELECT 5000000 + i FROM generate_series(1, 300) AS i")
    uids = [r[0] for r in await c.db.fetch("SELECT id FROM users WHERE tg_id >= 5000000 ORDER BY id")]
    async with c.sm() as s, s.begin():
        prev = await service.load_previous(s, uids)
        items = [service.Item(u, sid, RICH) for u in uids]
        results = {i.user_id: service.compute(i, prev, cheap=True) for i in items}
        assert sum(len(r["progress"]) + 1 for r in results.values()) * 6 > 32767
        out = await service.write_results(s, items, results, prev, via="backfill", evaluated_at=datetime.now(timezone.utc))
    assert sum(len(v) for v in out.values()) == 300 * len(RICH_IDS)
    assert await c.db.fetchval("SELECT count(*) FROM achievements_earned") == 300 * len(RICH_IDS)
    assert await c.db.fetchval("SELECT count(*) FROM achievement_progress WHERE achievement_id = '_counters'") == 300
    assert await c.db.fetchval("SELECT count(DISTINCT achievement_points) FROM player_scores WHERE user_id = ANY($1)", uids) == 1
