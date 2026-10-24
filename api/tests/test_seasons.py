"""S4 part B: server-side season rotation, state alignment with the mock's season block, /internal/* endpoints
(rotate, top advance, bots grow). Real Postgres + Redis (skipped when absent), in-process fake sim (tests/fake_sim.py)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from app import seasons
from app.seed_bots import seed
from tests.game_helpers import bootstrap, command, factory_row, key, me, place
from app.sim_client import now_ms
from tests.helpers import TEST_DB_URL, TEST_REDIS_URL, dev
from tests.test_errors import assert_error

TOKEN = "test-internal-token-0123456789"
H = 3_600_000
DAY = 24 * H
ROTATE, TOP, GROW = "/internal/seasons/rotate", "/internal/top/advance", "/internal/bots/grow"


@pytest_asyncio.fixture
async def srv(make_client, clean_db, clean_redis, fake_sim):
    client = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                               rate_limit_enabled=False, internal_token=TOKEN)
    client.db, client.redis, client.sim = clean_db, clean_redis, fake_sim  # type: ignore[attr-defined]
    return client


def post(c, path, body=None, token=TOKEN):
    headers = {"X-Internal-Token": token} if token is not None else {}
    return c.post(path, json=body, headers=headers)


async def rotate(c) -> dict:
    r = await post(c, ROTATE)
    assert r.status_code == 200, r.text
    return r.json()


async def set_season_end(db, sid: int, ago: str) -> None:
    """ends_at = now - `ago` (a Postgres interval, e.g. '30 minutes')."""
    await db.execute(f"UPDATE seasons SET ends_at = now() - interval '{ago}' WHERE id = $1", sid)


async def uid_of(db, tg: int) -> int:
    return await db.fetchval("SELECT id FROM users WHERE tg_id = $1", tg)


async def set_stars(db, tg: int, stars: int) -> None:
    """Column + the fake sim's own copy in `state.sim.stars` (a later write recomputes the column from the snapshot)."""
    await db.execute("UPDATE factories SET stars = $1, state = jsonb_set(state, '{sim}', "
                     "coalesce(state->'sim', '{}'::jsonb) || jsonb_build_object('stars', $1::int)) WHERE user_id = $2",
                     stars, await uid_of(db, tg))


async def player(c, tg: int, stars: int = 0) -> dict:
    body = await bootstrap(c, tg, f"P{tg}")
    if stars:
        await set_stars(c.db, tg, stars)
    return body


# ---------------------------------------------------------------- auth

@pytest.mark.parametrize("path", [ROTATE, TOP, GROW])
async def test_internal_requires_the_token(srv, path):
    for token in (None, "", "wrong-token", TOKEN + "x", TOKEN[:-1]):
        assert_error(await post(srv, path, token=token), 401, "unauthorized")
    assert (await post(srv, path)).status_code == 200


async def test_internal_is_disabled_without_a_configured_token(make_client, clean_db, clean_redis, fake_sim):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL)  # internal_token=""
    for token in (None, "", "anything"):
        assert_error(await post(c, ROTATE, token=token), 401, "unauthorized")


async def test_internal_routes_do_not_take_the_user_auth(srv):
    r = await srv.post(ROTATE, headers={**dev(1), "X-Internal-Token": TOKEN})
    assert r.status_code == 200 and r.json() == {"closed": [], "opened": None}


# ---------------------------------------------------------------- rotation

async def test_rotate_without_seasons_or_with_an_open_one_is_a_noop(srv):
    assert await rotate(srv) == {"closed": [], "opened": None}
    await player(srv, 1)
    assert await rotate(srv) == {"closed": [], "opened": None}
    assert [(r["id"], r["closed"]) for r in await srv.db.fetch("SELECT id, closed FROM seasons")] == [(1, False)]


async def test_rotate_closes_a_due_season_and_freezes_the_results(srv):
    for tg, stars in ((1, 500), (2, 900), (3, 500)):
        await player(srv, tg, stars)
    await seed_bots(srv, 2)
    bot = await srv.db.fetchrow("SELECT user_id, stars FROM factories WHERE is_bot ORDER BY stars DESC LIMIT 1")
    await srv.db.execute("UPDATE factories SET stars = 700, quality_avg = 0.9, speed_bph = 1200, bottles_season = 42 "
                         "WHERE user_id = $1", await uid_of(srv.db, 2))
    await set_season_end(srv.db, 1, "10 minutes")

    assert await rotate(srv) == {"closed": [1], "opened": None}  # ended 10 min ago: closed, the next one waits for 1 h
    s = await srv.db.fetchrow("SELECT closed, closed_at FROM seasons WHERE id = 1")
    assert s["closed"] and s["closed_at"] is not None
    rows = await srv.db.fetch("SELECT * FROM season_results WHERE season_id = 1 ORDER BY place")
    assert [r["place"] for r in rows] == list(range(1, len(rows) + 1)) and len(rows) == 5
    by_user = {r["user_id"]: r for r in rows}
    assert by_user[bot["user_id"]]["is_bot"] and by_user[bot["user_id"]]["stars"] == bot["stars"]
    # place = stars desc, ties by user id asc (the live leaderboard order), bots included
    expected = sorted(((r["stars"], r["user_id"]) for r in rows), key=lambda t: (-t[0], t[1]))
    assert [(r["stars"], r["user_id"]) for r in rows] == expected
    u2 = by_user[await uid_of(srv.db, 2)]
    assert (u2["stars"], u2["quality"], u2["speed_bph"], u2["bottles"], u2["is_bot"]) == (700, pytest.approx(0.9), 1200, 42, False)

    # frozen: later changes of the factories do not touch the results, a second call does nothing
    await srv.db.execute("UPDATE factories SET stars = 1")
    before = [dict(r) for r in rows]
    assert await rotate(srv) == {"closed": [], "opened": None}
    assert [dict(r) for r in await srv.db.fetch("SELECT * FROM season_results ORDER BY place")] == before


async def test_results_skip_bots_when_they_are_disabled(srv):
    await player(srv, 1, 5)
    await seed_bots(srv, 3)
    await set_season_end(srv.db, 1, "1 minute")
    async with srv.app.state.sessionmaker() as session, session.begin():
        assert (await seasons.rotate(session, now_ms(), bots_enabled=False)).closed == [1]
    assert await srv.db.fetchval("SELECT count(*) FROM season_results WHERE is_bot") == 0
    assert await srv.db.fetchval("SELECT count(*) FROM season_results") == 1


async def test_next_season_opens_only_one_hour_after_the_end(srv):
    await player(srv, 1)
    await set_season_end(srv.db, 1, "30 minutes")
    assert await rotate(srv) == {"closed": [1], "opened": None}
    await set_season_end(srv.db, 1, "59 minutes")
    assert await rotate(srv) == {"closed": [], "opened": None}
    assert await srv.db.fetchval("SELECT count(*) FROM seasons") == 1

    await set_season_end(srv.db, 1, "61 minutes")
    assert await rotate(srv) == {"closed": [], "opened": 2}
    s2 = await srv.db.fetchrow("SELECT *, ends_at - starts_at AS len, now() - starts_at AS age FROM seasons WHERE id = 2")
    assert not s2["closed"] and s2["days"] == 7 and s2["len"].days == 7 and s2["age"].total_seconds() < 30
    assert await rotate(srv) == {"closed": [], "opened": None}  # idempotent
    assert await srv.db.fetchval("SELECT count(*) FROM seasons") == 2


async def test_an_overdue_season_is_closed_and_succeeded_in_one_call(srv):
    await player(srv, 1, 3)
    await set_season_end(srv.db, 1, "5 hours")  # the job was down
    assert await rotate(srv) == {"closed": [1], "opened": 2}
    assert await srv.db.fetchval("SELECT count(*) FROM season_results WHERE season_id = 1") == 1


async def test_concurrent_rotations_do_the_work_once(srv):
    await player(srv, 1, 3)
    await set_season_end(srv.db, 1, "2 hours")
    out = await asyncio.gather(*(rotate(srv) for _ in range(5)))
    assert sorted(o["closed"] for o in out) == [[], [], [], [], [1]]
    assert sorted(str(o["opened"]) for o in out) == ["2", "None", "None", "None", "None"]
    assert await srv.db.fetchval("SELECT count(*) FROM seasons") == 2
    assert await srv.db.fetchval("SELECT count(*) FROM season_results") == 1


# ---------------------------------------------------------------- state alignment

async def test_fresh_factory_gets_the_server_season(srv):
    await srv.db.execute("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now() - interval '1 day', "
                         "now() + interval '2 days', 3)")
    body = await bootstrap(srv)
    season = body["season"]
    assert season["id"] == 1 and season["endsAt"] - season["startsAt"] == 3 * DAY
    row = await factory_row(srv)
    assert {k: row["state"]["season"][k] for k in ("id", "startsAt", "endsAt")} == season
    snap_season = body["snapshot"]["season"]
    assert (snap_season["id"], snap_season["startsAt"], snap_season["endsAt"]) == (1, season["startsAt"], season["endsAt"])
    assert snap_season["closed"] is False and snap_season["dayIndex"] == 2 and snap_season["days"] == 7
    assert row["season_id"] == 1


async def test_next_bootstrap_resets_the_season_part_of_the_state_and_keeps_the_factory(srv):
    first = await player(srv, 1, 500)
    uid = await uid_of(srv.db, 1)
    await srv.db.execute(
        "UPDATE factories SET state = state || $1::jsonb WHERE user_id = $2",
        json.dumps({"level": 4, "sim": {"stars": 500, "shipped": {"bottle": 10}, "rejected": 3, "credits": 123,
                            "layout": {"buildings": [{"id": "b1", "kind": "filler"}]}},
                    "season": {**first["snapshot"]["season"], "shiftsLeft": 2, "shiftsDay": "2026-10-24"}}), uid)
    before = await factory_row(srv)
    await set_season_end(srv.db, 1, "2 hours")
    assert await rotate(srv) == {"closed": [1], "opened": 2}
    assert (await srv.db.fetchval("SELECT season_id FROM factories WHERE user_id = $1", uid)) == 1  # moved lazily

    body = await bootstrap(srv, 1, "P1")
    assert body["season"]["id"] == 2 and body["snapshot"]["season"]["id"] == 2
    assert body["snapshot"]["factory"]["stars"] == 0 and body["version"] == before["version"]
    row = await factory_row(srv)
    st = row["state"]
    assert st["season"]["id"] == 2 and st["season"]["startsAt"] == body["season"]["startsAt"]
    assert st["season"]["endsAt"] == body["season"]["endsAt"]
    assert (st["season"]["shiftsLeft"], st["season"]["shiftsDay"]) == (2, "2026-10-24")  # untouched
    assert (st["sim"]["stars"], st["sim"]["shipped"], st["sim"]["rejected"]) == (0, {}, 0)
    assert st["sim"]["layout"] == {"buildings": [{"id": "b1", "kind": "filler"}]} and st["sim"]["credits"] == 123
    assert st["credits"] >= before["state"]["credits"] and st["name"] == before["state"]["name"]  # factory kept
    assert (row["season_id"], row["stars"], row["level"]) == (2, 0, 4)
    assert srv.sim.rolls == 0
    # the frozen result of season 1 still has the old stars
    assert await srv.db.fetchval("SELECT stars FROM season_results WHERE season_id = 1 AND user_id = $1", uid) == 500


@pytest.mark.parametrize("via", ["advance", "factory_me", "command"])
async def test_every_write_path_aligns_the_state(srv, via):
    await player(srv, 1, 40)
    await set_season_end(srv.db, 1, "2 hours")
    await rotate(srv)
    if via == "advance":
        r = await srv.post("/api/advance", json={"reason": "resume"}, headers=dev(1))
    elif via == "factory_me":
        r = await me(srv)
    else:
        r = await command(srv, 0, key(1), place())
    assert r.status_code == 200, r.text
    assert r.json()["snapshot"]["season"]["id"] == 2
    row = await factory_row(srv)
    assert row["state"]["season"]["id"] == 2 and row["season_id"] == 2 and row["stars"] == 0


async def test_the_mocks_own_season_roll_never_fires(srv):
    """Server is on season 3, the factory's state still says season 1 (closed 20 days ago): without the alignment the
    emulated mock would roll it to season 2 by itself."""
    await srv.db.execute("INSERT INTO seasons (starts_at, ends_at, days, closed) VALUES "
                         "(now() - interval '30 days', now() - interval '20 days', 7, true),"
                         "(now() - interval '19 days', now() - interval '10 days', 7, true),"
                         "(now() - interval '1 day', now() + interval '6 days', 7, false)")
    body = await bootstrap(srv)
    assert body["season"]["id"] == 3
    old = {**body["snapshot"]["season"], "id": 1, "startsAt": 1_000, "endsAt": 2_000}
    for call in (lambda: bootstrap(srv), lambda: srv.post("/api/advance", json={"reason": "resume"}, headers=dev(1)),
                 lambda: me(srv)):
        await srv.db.execute("UPDATE factories SET season_id = 1, state = jsonb_set(state, '{season}', $1::jsonb)",
                             json.dumps(old))  # back to the stale state before every kind of write
        r = await call()
        assert getattr(r, "status_code", 200) == 200
        assert (await factory_row(srv))["state"]["season"]["id"] == 3
    assert srv.sim.rolls == 0


async def test_gap_between_seasons_keeps_the_closed_one_and_the_mock_does_not_roll(srv):
    await player(srv, 1, 10)
    await set_season_end(srv.db, 1, "30 minutes")
    assert await rotate(srv) == {"closed": [1], "opened": None}
    body = await bootstrap(srv, 1, "P1")  # inside the 1 h gap: still season 1 (closed by its end time), no new one
    assert body["season"]["id"] == 1 and srv.sim.rolls == 0
    row = await factory_row(srv)
    assert row["state"]["season"]["endsAt"] == body["season"]["endsAt"]
    assert await srv.db.fetchval("SELECT count(*) FROM seasons") == 1


async def test_write_after_the_roll_time_opens_the_next_season_inline(srv):
    """The job has not run for 2 h: the first request rotates the seasons itself (the mock would roll at the same
    moment) and the sim never sees a stale season."""
    await player(srv, 1, 10)
    await set_season_end(srv.db, 1, "2 hours")
    r = await me(srv)
    assert r.status_code == 200 and r.json()["snapshot"]["season"]["id"] == 2
    assert [(x["id"], x["closed"]) for x in await srv.db.fetch("SELECT id, closed FROM seasons ORDER BY id")] == \
        [(1, True), (2, False)]
    assert await srv.db.fetchval("SELECT count(*) FROM season_results WHERE season_id = 1") == 1
    assert srv.sim.rolls == 0


def test_align_state_is_pure_and_keeps_what_the_mock_keeps():
    season = SimpleNamespace(id=4, starts_at=_dt(1_000_000), ends_at=_dt(9_000_000))
    state = {"season": {"id": 3, "startsAt": 1, "endsAt": 2, "shiftsLeft": 1, "shiftsDay": "d"},
             "sim": {"stars": 9, "shipped": {"a": 1}, "rejected": 2, "credits": 5, "layout": {"x": 1}}, "meta": {"k": 1}}
    assert seasons.align_state(state, season) is True
    assert state["season"] == {"id": 4, "startsAt": 1_000_000, "endsAt": 9_000_000, "shiftsLeft": 1, "shiftsDay": "d"}
    assert state["sim"] == {"stars": 0, "shipped": {}, "rejected": 0, "credits": 5, "layout": {"x": 1}}
    assert seasons.align_state(state, season) is False  # idempotent
    state["season"]["endsAt"] = 1  # same id, other bounds: bounds only, no reset
    state["sim"]["stars"] = 7
    assert seasons.align_state(state, season) is True and state["sim"]["stars"] == 7
    assert seasons.align_state({"sim": {}}, season) is False  # no season block: left alone


def _dt(t_ms: int):
    return seasons.to_dt(t_ms)


# ---------------------------------------------------------------- top advance

def bg_calls(sim):
    return [c["body"] for c in sim.calls if c["path"] == "/v1/advance" and c["body"].get("reason") == "background"]


async def test_top_advance_takes_the_top_n_real_players_without_touching_versions(srv):
    for tg, stars in ((1, 10), (2, 50), (3, 30), (4, 20)):
        await player(srv, tg, stars)
    await seed_bots(srv, 3)
    versions = {tg: (await factory_row(srv, tg))["version"] for tg in (1, 2, 3, 4)}
    srv.sim.calls.clear()
    r = await post(srv, TOP, {"limit": 2})
    out = r.json()
    assert r.status_code == 200 and (out["advanced"], out["skipped"], out["failed"], out["sim_down"]) == (2, 0, 0, False)
    assert out["ms"] >= 0 and out["pending"] == 0
    calls = bg_calls(srv.sim)
    assert [c["state"]["player"]["name"] for c in calls] == ["P2", "P3"]  # top by stars, bots never
    assert all(c["capHours"] == 8 for c in calls)
    assert {tg: (await factory_row(srv, tg))["version"] for tg in (1, 2, 3, 4)} == versions

    srv.sim.calls.clear()
    out = (await post(srv, TOP)).json()  # default limit 200: every real player, no bots
    assert out["advanced"] == 4 and len(bg_calls(srv.sim)) == 4
    assert {tg: (await factory_row(srv, tg))["version"] for tg in (1, 2, 3, 4)} == versions


async def test_top_advance_updates_the_denormalised_columns(srv):
    await player(srv, 1)
    long_ago = await srv.db.fetchval("SELECT (extract(epoch FROM now()) * 1000)::bigint - 3600000")
    await srv.db.execute("UPDATE factories SET state = jsonb_set(state, '{lastTick}', to_jsonb($1::bigint))", long_ago)
    credits = (await factory_row(srv))["credits"]
    assert (await post(srv, TOP)).json()["advanced"] == 1
    row = await factory_row(srv)
    assert row["credits"] > credits and row["state"]["lastTick"] > long_ago and row["snapshot"] is not None


async def test_top_advance_skips_a_locked_row_and_never_blocks_or_conflicts(srv):
    for tg, stars in ((1, 30), (2, 20), (3, 10)):
        await player(srv, tg, stars)
    uid = await uid_of(srv.db, 1)
    async with srv.db.transaction():
        await srv.db.fetchval("SELECT 1 FROM factories WHERE user_id = $1 FOR UPDATE", uid)  # a request in flight
        # (a second connection: the fixture's own connection holds the lock, the job needs its own one)
        out = (await asyncio.wait_for(post(srv, TOP), timeout=10)).json()
    assert (out["advanced"], out["skipped"], out["failed"]) == (2, 1, 0)
    assert (await post(srv, TOP)).json()["advanced"] == 3  # the lock is gone: caught up next run


async def test_top_advance_does_not_409_a_command_in_flight(srv):
    await player(srv, 1, 5)
    srv.sim.delay = 0.4  # the command holds the row lock for the length of its sim call
    cmd = asyncio.create_task(command(srv, 0, key(1), place()))
    await asyncio.sleep(0.15)
    out = (await post(srv, TOP)).json()
    assert (out["advanced"], out["skipped"]) == (0, 1)
    r = await cmd
    assert r.status_code == 200 and r.json()["version"] == 1
    srv.sim.delay = 0
    assert (await post(srv, TOP)).json()["advanced"] == 1
    assert (await factory_row(srv))["version"] == 1  # still 1: the background run did not bump it
    r2 = await command(srv, 1, key(2), place(2, 2))  # a client that last saw version 1 is not rejected
    assert r2.status_code == 200 and r2.json()["version"] == 2


async def test_top_advance_stops_when_the_sim_is_down(srv):
    for tg in (1, 2, 3):
        await player(srv, tg, tg)
    srv.sim.down = True
    r = await post(srv, TOP)
    out = r.json()
    assert r.status_code == 200 and out["sim_down"] is True and out["advanced"] == 0
    assert out["failed"] == 1 and out["pending"] == 2  # stopped at the first failure, the rest was not tried
    srv.sim.down = False
    assert (await post(srv, TOP)).json()["advanced"] == 3


async def test_top_advance_in_the_gap_does_nothing(srv):
    await player(srv, 1, 5)
    await set_season_end(srv.db, 1, "10 minutes")
    await rotate(srv)
    srv.sim.calls.clear()
    out = (await post(srv, TOP)).json()
    assert out["advanced"] == 0 and out["season_open"] is False and bg_calls(srv.sim) == []


async def test_top_advance_validates_the_limit(srv):
    for bad in (0, 1001, -1, "x"):
        assert_error(await post(srv, TOP, {"limit": bad}), 422, "validation_error")


async def test_top_advance_moves_a_stale_factory_into_the_new_season(srv):
    """Rotation does not touch factories; the background run of a player of the closed season is not counted for the
    new one (it is not in it yet), a player who came back is."""
    await player(srv, 1, 7)
    await player(srv, 2, 9)
    await set_season_end(srv.db, 1, "2 hours")
    await rotate(srv)
    await bootstrap(srv, 2, "P2")  # player 2 is back in season 2
    srv.sim.calls.clear()
    out = (await post(srv, TOP)).json()
    assert out["season_id"] == 2 and out["advanced"] == 1
    assert [c["state"]["player"]["name"] for c in bg_calls(srv.sim)] == ["P2"]


# ---------------------------------------------------------------- bots

async def seed_bots(c, count: int, progress: float = 0.5) -> None:
    async with c.app.state.sessionmaker() as session, session.begin():
        await seed(session, count, progress)


async def bot_stars(db) -> dict[int, int]:
    return {r["user_id"]: r["stars"] for r in await db.fetch("SELECT user_id, stars FROM factories WHERE is_bot")}


async def test_bots_grow_is_monotonic_inside_a_season(srv):
    await player(srv, 1)
    await srv.db.execute("UPDATE seasons SET starts_at = now() - interval '1 day', ends_at = now() + interval '6 days'")
    await seed_bots(srv, 6, progress=0.1)
    s0 = await bot_stars(srv.db)
    r = await post(srv, GROW)
    out = r.json()
    assert r.status_code == 200 and out["bots"] == 6 and out["season_id"] == 1 and 0.13 < out["progress"] < 0.15
    s1 = await bot_stars(srv.db)
    assert all(s1[k] >= s0[k] for k in s0) and any(s1[k] > s0[k] for k in s0)  # 1/7 > 0.1 of the season

    await srv.db.execute("UPDATE seasons SET starts_at = now() - interval '5 days', ends_at = now() + interval '2 days'")
    assert (await post(srv, GROW)).status_code == 200
    s2 = await bot_stars(srv.db)
    assert all(s2[k] >= s1[k] for k in s1) and any(s2[k] > s1[k] for k in s1)

    full = "SELECT user_id, stars, credits, xp, bottles_season, speed_bph, quality_avg FROM factories WHERE is_bot ORDER BY 1"
    snap = [tuple(r) for r in await srv.db.fetch(full)]
    await srv.db.execute("UPDATE seasons SET starts_at = now(), ends_at = now() + interval '7 days'")  # progress back to 0
    assert (await post(srv, GROW)).status_code == 200
    assert [tuple(r) for r in await srv.db.fetch(full)] == snap  # nothing goes down inside the season


async def test_bots_grow_waits_while_no_season_is_open(srv):
    await player(srv, 1)
    await seed_bots(srv, 3)
    await set_season_end(srv.db, 1, "10 minutes")
    await rotate(srv)
    out = (await post(srv, GROW)).json()
    assert out["bots"] == 0 and out["skipped"] == "no_open_season"


async def test_a_new_season_brings_the_bots_along(srv):
    await player(srv, 1)
    await seed_bots(srv, 4, progress=0.9)
    old = await bot_stars(srv.db)
    await set_season_end(srv.db, 1, "2 hours")
    assert await rotate(srv) == {"closed": [1], "opened": 2}
    assert await srv.db.fetchval("SELECT count(*) FROM factories WHERE is_bot AND season_id = 2") == 4
    new = await bot_stars(srv.db)
    assert all(new[k] < old[k] for k in old if old[k] > 5)  # a new season starts from the bottom of the curve
    assert await srv.db.fetchval("SELECT count(*) FROM season_results WHERE season_id = 1 AND is_bot") == 4
    lb = await srv.get("/api/season/leaderboard", headers=dev(1))
    assert lb.json()["seasonId"] == 2
