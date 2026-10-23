"""Row locking, create-on-first-bootstrap races, idempotency under concurrency and 429 on every game route.
Real Postgres + Redis, in-process fake sim (tests/fake_sim.py)."""
from __future__ import annotations

import asyncio

import pytest

from tests.fake_sim import PLACE_COST, START_CREDITS
from tests.game_helpers import advance, bootstrap, command, factory_row, key, me, place
from tests.helpers import TEST_DB_URL, TEST_REDIS_URL, dev
from tests.test_errors import assert_error


# ================================================================ concurrent commands

async def test_two_concurrent_commands_with_the_same_version_exactly_one_wins(game, fake_sim):
    await bootstrap(game)
    fake_sim.delay = 0.15  # both requests are inside the handler at the same time
    a, b = await asyncio.gather(command(game, 0, key(1), place(1, 1)), command(game, 0, key(2), place(2, 2)))
    assert sorted([a.status_code, b.status_code]) == [200, 409]
    loser = a if a.status_code == 409 else b
    winner = b if loser is a else a
    assert winner.json()["version"] == 1 and winner.json()["replayed"] is False
    assert assert_error(loser, 409, "version_conflict")["details"] == {"current_version": 1}
    row = await factory_row(game)
    assert row["version"] == 1 and row["credits"] == START_CREDITS - PLACE_COST  # applied exactly once
    assert len(row["state"]["buildings"]) == 1
    assert len(fake_sim.bodies("/v1/command")) == 1  # the loser never reached the sim


async def test_many_concurrent_commands_serialise_on_the_row_lock(game, fake_sim):
    await bootstrap(game)
    fake_sim.delay = 0.05
    results = await asyncio.gather(*[command(game, 0, key(i), place(i, i)) for i in range(6)])
    codes = sorted(r.status_code for r in results)
    assert codes == [200] + [409] * 5
    assert (await factory_row(game))["version"] == 1


async def test_chained_commands_from_two_tabs_resync_via_409(game, fake_sim):
    """Tab A and B both act on version 0: one wins, the loser resyncs through factory/me and retries."""
    await bootstrap(game)
    fake_sim.delay = 0.1
    a, b = await asyncio.gather(command(game, 0, key("a"), place(1, 1)), command(game, 0, key("b"), place(2, 2)))
    loser_key, loser_cmd = (key("a"), place(1, 1)) if a.status_code == 409 else (key("b"), place(2, 2))
    fake_sim.delay = 0
    synced = (await me(game)).json()
    assert synced["version"] == 1 and synced["degraded"] is False
    retry = await command(game, synced["version"], loser_key, loser_cmd)
    assert retry.status_code == 200 and retry.json()["version"] == 2
    assert (await factory_row(game))["credits"] == START_CREDITS - 2 * PLACE_COST


async def test_concurrent_commands_with_the_same_key_apply_once(game, fake_sim):
    await bootstrap(game)
    fake_sim.delay = 0.15
    a, b = await asyncio.gather(command(game, 0, key(1), place()), command(game, 0, key(1), place()))
    assert a.status_code == b.status_code == 200
    assert sorted([a.json()["replayed"], b.json()["replayed"]]) == [False, True]
    assert a.json()["version"] == b.json()["version"] == 1
    assert (await factory_row(game))["credits"] == START_CREDITS - PLACE_COST  # charged once
    assert len(fake_sim.bodies("/v1/command")) == 1


async def test_catch_up_requests_wait_for_a_command_but_never_conflict(game, fake_sim):
    await bootstrap(game)
    fake_sim.delay = 0.1
    cmd, adv, mine, boot = await asyncio.gather(
        command(game, 0, key(1), place()), advance(game), me(game), game.get("/api/bootstrap", headers=dev(1)))
    assert cmd.status_code == adv.status_code == mine.status_code == boot.status_code == 200
    assert (await factory_row(game))["version"] == 1  # only the command bumped it
    for r in (adv, mine, boot):
        assert r.json()["version"] in (0, 1)


# ================================================================ create-on-first-bootstrap races

async def test_concurrent_first_bootstraps_of_one_user_create_one_factory(game, fake_sim):
    fake_sim.delay = 0.1
    results = await asyncio.gather(*[game.get("/api/bootstrap", headers=dev(1)) for _ in range(4)])
    assert all(r.status_code == 200 and r.json()["version"] == 0 for r in results)
    assert (await game.db.fetchval("SELECT count(*) FROM factories")) == 1
    assert (await game.db.fetchval("SELECT count(*) FROM seasons")) == 1
    created = [c for c in fake_sim.bodies("/v1/bootstrap") if c["state"] is None]
    assert len(created) == 1  # the others found the committed state and only caught up


async def test_first_bootstraps_of_many_users_share_one_season(game):
    results = await asyncio.gather(*[game.get("/api/bootstrap", headers=dev(i)) for i in range(1, 5)])
    assert all(r.status_code == 200 for r in results)
    assert (await game.db.fetchval("SELECT count(*) FROM seasons")) == 1
    assert (await game.db.fetchval("SELECT count(*) FROM factories")) == 4
    assert len({r.json()["season"]["id"] for r in results}) == 1


async def test_users_do_not_block_each_other(game, fake_sim):
    await bootstrap(game, 1)
    await bootstrap(game, 2)
    fake_sim.delay = 0.4
    t0 = asyncio.get_running_loop().time()
    a, b = await asyncio.gather(command(game, 0, key(1), place(), tg=1), command(game, 0, key(2), place(), tg=2))
    assert a.status_code == b.status_code == 200
    assert asyncio.get_running_loop().time() - t0 < 0.78  # parallel, not 2 x 0.4 s


# ================================================================ 429 on every route

LIMITED = [
    ("bootstrap", "GET", "/api/bootstrap", None),
    ("command", "POST", "/api/command",
     {"version": 0, "idempotency_key": "idem-key-0001", "cmd": {"type": "buildEnd"}}),
    ("advance", "POST", "/api/advance", {}),
    ("factory_me", "GET", "/api/factory/me", None),
]


@pytest.mark.parametrize("name,method,path,body", LIMITED)
async def test_rate_limited_with_retry_after(make_client, clean_db, clean_redis, fake_sim, name, method, path, body):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                          rate_limit_overrides={name: 2})
    c.db = clean_db
    if name != "bootstrap":
        assert (await c.get("/api/bootstrap", headers=dev(1))).status_code == 200  # own limit, not counted here
    for _ in range(2):
        resp = await c.request(method, path, json=body, headers=dev(1))
        assert resp.status_code == 200, resp.text
    resp = await c.request(method, path, json=body, headers=dev(1))
    err = assert_error(resp, 429, "rate_limited")
    retry_after = int(resp.headers["retry-after"])
    assert 1 <= retry_after <= 60 and err["details"]["retry_after"] == retry_after and err["details"]["limit"] == 2
    # another player is unaffected (limits are per user)
    other = await c.request(method, path, json=body, headers=dev(2))
    assert other.status_code != 429
    # the other routes keep their own counters
    others = [x for x in LIMITED if x[0] != name]
    assert (await c.request(others[0][1], others[0][2], json=others[0][3], headers=dev(1))).status_code != 429


async def test_unauthenticated_floods_are_counted_per_ip_on_game_routes(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"bootstrap": 2})
    assert (await c.get("/api/bootstrap")).status_code == 401
    assert (await c.get("/api/bootstrap")).status_code == 401
    assert_error(await c.get("/api/bootstrap"), 429, "rate_limited")
