"""GET /api/bootstrap, POST /api/command, POST /api/advance, GET /api/factory/me against real Postgres + Redis and an
in-process fake sim (tests/fake_sim.py). Concurrency / rate limits: tests/test_game_concurrency.py."""
from __future__ import annotations

import time

import pytest

from app.contract.generated import COMMAND_TYPES
from tests.fake_sim import PLACE_COST, START_CREDITS
from tests.game_helpers import Clock, advance, bootstrap, command, factory_row, key, me, place, shift
from tests.helpers import DEAD_REDIS_URL, TEST_DB_URL, dev, tma
from tests.test_errors import assert_error

GOOD_CMD = {"version": 0, "idempotency_key": "idem-key-0001", "cmd": {"type": "ageConfirm"}}
ROUTES = [("GET", "/api/bootstrap", None), ("POST", "/api/command", GOOD_CMD),
          ("POST", "/api/advance", {}), ("GET", "/api/factory/me", None)]


@pytest.fixture
def clock(monkeypatch):
    c = Clock(int(time.time() * 1000))
    monkeypatch.setattr("app.game.now_ms", c)
    return c


# ================================================================ 401 on every endpoint

@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_401_missing_header(make_client, method, path, body):
    c = await make_client()
    assert_error(await c.request(method, path, json=body), 401, "unauthorized")


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_401_invalid_and_expired_init_data(make_client, method, path, body):
    c = await make_client()
    bad = await c.request(method, path, json=body, headers={"Authorization": "tma not-init-data"})
    assert_error(bad, 401, "invalid_init_data")
    forged = await c.request(method, path, json=body, headers=tma(token="999:OTHER-TOKEN"))
    assert_error(forged, 401, "invalid_init_data")
    old = await c.request(method, path, json=body, headers=tma(auth_date=int(time.time()) - 10 * 86400))
    assert_error(old, 401, "auth_expired")


async def test_401_wins_over_body_validation(make_client):
    c = await make_client()
    resp = await c.post("/api/command", json={"nonsense": True})
    assert_error(resp, 401, "unauthorized")


# ================================================================ GET /api/bootstrap

async def test_bootstrap_creates_factory(game, fake_sim):
    resp = await game.get("/api/bootstrap", headers=dev(7, "Ann"))
    assert resp.status_code == 200 and resp.headers["content-type"] == "application/json"
    body = resp.json()
    assert set(body) == {"version", "serverTime", "season", "features", "snapshot"}
    assert body["version"] == 0
    assert body["features"] == {"undo": False, "serverOrderEta": False}
    assert set(body["season"]) == {"id", "startsAt", "endsAt"}
    assert body["season"]["endsAt"] - body["season"]["startsAt"] == 7 * 86_400_000
    assert body["snapshot"]["factory"]["name"] == "Ann" and body["snapshot"]["factory"]["credits"] == START_CREDITS
    assert isinstance(body["serverTime"], int)

    row = await factory_row(game, 7)
    assert row["version"] == 0 and row["season_id"] == body["season"]["id"]
    assert row["snapshot"] == body["snapshot"]  # the degraded-mode snapshot is stored
    assert row["state"]["player"]["tgId"] == 7 and row["state"]["player"]["name"] == "Ann"
    assert (await game.db.fetchval("SELECT count(*) FROM seasons")) == 1
    # fresh factory: the sim was asked with state=null and the player
    assert fake_sim.paths() == ["/v1/bootstrap"]
    sent = fake_sim.calls[0]["body"]
    assert sent["state"] is None and sent["player"]["tgId"] == 7 and sent["player"]["name"] == "Ann"


async def test_bootstrap_is_idempotent_and_catches_up_without_version_bump(game, fake_sim, clock):
    first = await bootstrap(game)
    clock.t += 60_000
    second = await bootstrap(game)
    assert second["version"] == 0 and second["snapshot"]["factory"]["credits"] == START_CREDITS + 60
    assert second["season"] == first["season"]
    assert (await game.db.fetchval("SELECT count(*) FROM factories")) == 1
    existing = fake_sim.calls[1]["body"]  # second call carries the stored state, not null
    assert existing["state"]["lastTick"] == clock.t - 60_000 and existing["now"] == clock.t
    row = await factory_row(game)
    assert row["version"] == 0 and row["credits"] == START_CREDITS + 60  # persisted


async def test_bootstrap_never_409_after_commands(game):
    await bootstrap(game)
    assert (await command(game, 0, key(1), place())).status_code == 200
    again = await game.get("/api/bootstrap", headers=dev(1))
    assert again.status_code == 200 and again.json()["version"] == 1


async def test_bootstrap_uses_existing_open_season(game):
    await game.db.execute("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '3 days', 3)")
    body = await bootstrap(game)
    assert body["season"]["id"] == 1 and (await game.db.fetchval("SELECT count(*) FROM seasons")) == 1


async def test_bootstrap_creates_season_when_all_are_closed(game):
    await game.db.execute("INSERT INTO seasons (starts_at, ends_at, days, closed) "
                          "VALUES (now(), now() + interval '7 days', 7, true)")
    body = await bootstrap(game)
    assert body["season"]["id"] == 2
    assert [r["closed"] for r in await game.db.fetch("SELECT closed FROM seasons ORDER BY id")] == [True, False]


async def test_bootstrap_sim_down_503_and_leaves_no_factory(game, fake_sim):
    fake_sim.down = True
    resp = await game.get("/api/bootstrap", headers=dev(1))
    assert_error(resp, 503, "sim_unavailable")
    assert (await game.db.fetchval("SELECT count(*) FROM factories")) == 0  # rolled back with the transaction
    assert (await game.db.fetchval("SELECT count(*) FROM seasons")) == 0
    fake_sim.down = False
    assert (await bootstrap(game))["version"] == 0  # recovers


@pytest.mark.parametrize("mode", ["status", "garbage"])
async def test_bootstrap_bad_sim_answers_are_503(game, fake_sim, mode):
    if mode == "status":
        fake_sim.status = 502
    else:
        fake_sim.garbage = {"snapshot": {}}  # no `state`
    assert_error(await game.get("/api/bootstrap", headers=dev(1)), 503, "sim_unavailable")


# ================================================================ POST /api/command: success

async def test_command_success_shape_and_persistence(game, fake_sim, clock):
    await bootstrap(game)
    resp = await command(game, 0, key(1), place(3, 4))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "version", "result", "events", "snapshot", "replayed"}
    assert body["ok"] is True and body["version"] == 1 and body["replayed"] is False and body["events"] == []
    assert body["result"] == {"toast": "Построено: filler"}
    assert body["snapshot"]["factory"]["credits"] == START_CREDITS - PLACE_COST

    sent = fake_sim.bodies("/v1/command")[0]
    assert sent["cmd"] == place(3, 4) and sent["now"] == clock.t and sent["state"]["credits"] == START_CREDITS
    row = await factory_row(game)
    assert row["version"] == 1 and row["state"]["credits"] == START_CREDITS - PLACE_COST
    assert row["state"]["meta"]["idempotency"] == [
        {"key": key(1), "ok": True, "version": 1, "result": {"toast": "Построено: filler"}, "at": clock.t}]
    assert row["snapshot"] == body["snapshot"]


async def test_command_result_carries_rewards(game):
    await bootstrap(game)
    body = (await command(game, 0, key(1), shift())).json()
    assert body["result"] == {"toast": "Буст", "rewards": {"xp": 10}}


async def test_denormalised_columns_follow_every_write(game, clock):
    await bootstrap(game, name="Bob")
    row = await factory_row(game)
    assert (row["name"], row["credits"], row["stars"], row["level"], row["xp"], row["bottles_season"]) == \
           ("Bob", START_CREDITS, 0, 1, 0, 0)
    assert row["quality_avg"] == pytest.approx(0.5)
    assert int(row["last_tick_at"].timestamp() * 1000) == clock.t
    clock.t += 10_000
    assert (await advance(game)).status_code == 200
    row = await factory_row(game)
    assert (row["credits"], row["bottles_season"]) == (START_CREDITS + 10, 10)
    assert int(row["last_tick_at"].timestamp() * 1000) == clock.t
    assert (await command(game, 0, key(1), place())).status_code == 200
    assert (await command(game, 1, key(2), {"type": "renameFactory", "name": "Квас-Завод"})).status_code == 200
    assert (await command(game, 2, key(3), shift())).status_code == 200
    row = await factory_row(game)
    assert (row["name"], row["credits"], row["xp"], row["version"]) == ("Квас-Завод", START_CREDITS + 10 - PLACE_COST, 10, 3)
    assert row["snapshot"]["factory"]["name"] == "Квас-Завод"
    before = row["updated_at"]
    await advance(game)
    assert (await factory_row(game))["updated_at"] >= before


async def test_command_before_bootstrap_is_404(game, fake_sim):
    err = assert_error(await command(game, 0, key(1), place()), 404, "factory_not_found")
    assert err["details"] == {}
    assert fake_sim.paths() == []


# ================================================================ POST /api/command: 409 and idempotency

async def test_stale_version_is_409_with_current_version(game, fake_sim):
    await bootstrap(game)
    assert (await command(game, 0, key(1), place())).status_code == 200
    resp = await command(game, 0, key(2), place(2, 2))  # server is at 1
    err = assert_error(resp, 409, "version_conflict")
    assert err["details"] == {"current_version": 1}
    assert len(fake_sim.bodies("/v1/command")) == 1  # never reached the sim
    row = await factory_row(game)
    assert row["version"] == 1 and row["credits"] == START_CREDITS - PLACE_COST
    assert [e["key"] for e in row["state"]["meta"]["idempotency"]] == [key(1)]  # the failed key is not recorded
    future = await command(game, 5, key(3), place(2, 2))
    assert assert_error(future, 409, "version_conflict")["details"] == {"current_version": 1}
    # after resync the same key succeeds
    assert (await command(game, 1, key(2), place(2, 2))).json()["version"] == 2


async def test_replay_returns_stored_result_and_does_not_apply_twice(game, fake_sim):
    await bootstrap(game)
    first = (await command(game, 0, key(1), place())).json()
    again = await command(game, 0, key(1), place())  # same version AND same key: stale by now, but a replay
    assert again.status_code == 200
    body = again.json()
    assert body["replayed"] is True and body["ok"] is True and body["version"] == 1
    assert body["result"] == first["result"] and body["events"] == []
    assert body["snapshot"]["factory"]["credits"] == START_CREDITS - PLACE_COST  # not deducted twice
    assert len(fake_sim.bodies("/v1/command")) == 1
    assert (await factory_row(game))["credits"] == START_CREDITS - PLACE_COST


async def test_replay_with_stale_version_and_after_other_commands(game):
    await bootstrap(game)
    await command(game, 0, key(1), place())
    await command(game, 1, key(2), place(2, 2))
    replay = await command(game, 0, key(1), place())  # stale version, but the key is known
    body = replay.json()
    assert replay.status_code == 200 and body["replayed"] is True
    assert body["version"] == 2  # the CURRENT version, not the one stored with the key
    assert body["snapshot"]["factory"]["credits"] == START_CREDITS - 2 * PLACE_COST


async def test_replay_from_redis_fast_path_without_touching_the_row(game, fake_sim):
    await bootstrap(game)
    await command(game, 0, key(1), place())
    uid = await game.db.fetchval("SELECT id FROM users WHERE tg_id = 1")
    ttl = await game.redis.ttl(f"idem:{uid}:{key(1)}")
    assert 0 < ttl <= 600
    # wipe the durable log: if the replay still works it came from Redis
    await game.db.execute("UPDATE factories SET state = jsonb_set(state, '{meta,idempotency}', '[]')")
    assert (await command(game, 0, key(1), place())).json()["replayed"] is True
    await game.redis.flushdb()  # now the key is unknown everywhere -> a normal (stale) command
    assert_error(await command(game, 0, key(1), place()), 409, "version_conflict")
    assert len(fake_sim.bodies("/v1/command")) == 1


async def test_replay_from_deque_when_redis_is_empty(game):
    await bootstrap(game)
    await command(game, 0, key(1), place())
    await game.redis.flushdb()
    body = (await command(game, 0, key(1), place())).json()
    assert body["replayed"] is True and body["version"] == 1


async def test_idempotency_keys_are_per_user(game):
    await bootstrap(game, 1)
    await bootstrap(game, 2)
    assert (await command(game, 0, key(1), place(), tg=1)).json()["replayed"] is False
    other = await command(game, 0, key(1), place(), tg=2)  # same key, other player: applied, not a replay
    assert other.status_code == 200 and other.json()["replayed"] is False and other.json()["version"] == 1


async def test_failed_sim_call_does_not_burn_the_key(game, fake_sim):
    await bootstrap(game)
    fake_sim.down = True
    assert_error(await command(game, 0, key(1), place()), 503, "sim_unavailable")
    row = await factory_row(game)
    assert row["version"] == 0 and "meta" not in row["state"]  # nothing written
    fake_sim.down = False
    ok = await command(game, 0, key(1), place())  # the retry with the SAME key and version applies normally
    assert ok.status_code == 200 and ok.json()["replayed"] is False and ok.json()["version"] == 1


@pytest.mark.parametrize("mode", ["status", "garbage"])
async def test_command_bad_sim_answers_are_503(game, fake_sim, mode):
    await bootstrap(game)
    if mode == "status":
        fake_sim.status = 500
    else:
        fake_sim.garbage = {"state": {}, "snapshot": {}}  # no `result`
    assert_error(await command(game, 0, key(1), place()), 503, "sim_unavailable")
    assert (await factory_row(game))["version"] == 0


async def test_idempotency_deque_is_capped_at_50_and_touches_nothing_else(game, clock):
    await bootstrap(game)
    before = (await factory_row(game))["state"]
    for i in range(55):  # dialogDone is accepted by the fake without any visible change; the clock is frozen
        r = await command(game, i, key(i), {"type": "dialogDone", "id": "d"})
        assert r.status_code == 200 and r.json()["version"] == i + 1
    row = await factory_row(game)
    entries = row["state"]["meta"]["idempotency"]
    assert [e["key"] for e in entries] == [key(i) for i in range(5, 55)]
    assert [e["version"] for e in entries] == list(range(6, 56))
    assert row["version"] == 55
    assert {k: v for k, v in row["state"].items() if k != "meta"} == before  # only meta.idempotency changed
    # an evicted key is no longer a replay once the Redis cache (TTL 600 s) has forgotten it too
    await game.redis.flushdb()
    assert_error(await command(game, 0, key(0), {"type": "dialogDone", "id": "d"}), 409, "version_conflict")
    # a kept one still is
    assert (await command(game, 0, key(54), {"type": "dialogDone", "id": "d"})).json()["replayed"] is True


async def test_other_meta_fields_survive(game):
    await bootstrap(game)
    await game.db.execute("UPDATE factories SET state = jsonb_set(state, '{meta}', '{\"flags\": [1, 2]}')")
    await command(game, 0, key(1), place())
    meta = (await factory_row(game))["state"]["meta"]
    assert meta["flags"] == [1, 2] and len(meta["idempotency"]) == 1


async def test_redis_down_fails_open(make_client, clean_db, fake_sim):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=DEAD_REDIS_URL,
                          rate_limit_enabled=False)
    c.db = clean_db
    await bootstrap(c)
    ok = await command(c, 0, key(1), place())
    assert ok.status_code == 200 and ok.json()["version"] == 1
    replay = await command(c, 0, key(1), place())  # served from the durable deque
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert (await factory_row(c))["credits"] == START_CREDITS - PLACE_COST


# ================================================================ POST /api/command: rejections

async def test_rejected_command_is_422_with_details_and_recorded(game, fake_sim, clock):
    await bootstrap(game)
    await command(game, 0, key(1), place())
    clock.t += 5_000  # the catch-up part of a rejected command is persisted
    order = {"type": "acceptOrder", "dockId": "d1", "orderId": "o1"}
    err = assert_error(await command(game, 1, key(2), order), 422, "command_rejected")
    d = err["details"]
    assert d["reason"] == "rejected"  # the fake sends no `code`: fallback
    assert d["message"] == err["message"] == "Заказ недоступен"
    assert d["version"] == 1 and d["snapshot"]["factory"]["credits"] == START_CREDITS - PLACE_COST + 5
    assert "replayed" not in d
    row = await factory_row(game)
    assert row["version"] == 1  # no bump
    assert row["credits"] == START_CREDITS - PLACE_COST + 5  # catch-up persisted
    last = row["state"]["meta"]["idempotency"][-1]
    assert (last["key"], last["ok"], last["code"], last["version"]) == (key(2), False, "rejected", 1)
    assert last["result"] == {"message": "Заказ недоступен"}
    # the next command still uses version 1
    assert (await command(game, 1, key(3), place(2, 2))).json()["version"] == 2


async def test_rejected_command_replay_returns_the_same_rejection(game, fake_sim):
    await bootstrap(game)
    order = {"type": "acceptOrder", "dockId": "d1", "orderId": "o1"}
    assert_error(await command(game, 0, key(1), order), 422, "command_rejected")
    await command(game, 0, key(2), place())  # version moves on
    replay = await command(game, 0, key(1), order)
    err = assert_error(replay, 422, "command_rejected")
    assert err["details"]["replayed"] is True and err["details"]["reason"] == "rejected"
    assert err["details"]["message"] == "Заказ недоступен" and err["details"]["version"] == 1
    assert err["details"]["snapshot"]["factory"]["credits"] == START_CREDITS - PLACE_COST
    assert len(fake_sim.bodies("/v1/command")) == 2  # the replay did not reach the sim command endpoint
    await game.redis.flushdb()  # and the durable deque gives the same answer
    again = assert_error(await command(game, 7, key(1), order), 422, "command_rejected")
    assert again["details"]["replayed"] is True


async def test_rule_rejection_reason_insufficient_funds(game):
    await bootstrap(game)
    await game.db.execute("UPDATE factories SET state = jsonb_set(state, '{credits}', '50')")
    err = assert_error(await command(game, 0, key(1), place()), 422, "command_rejected")
    assert err["details"]["reason"] == "insufficient_funds" and err["details"]["version"] == 0


async def test_daily_limit_exceeded_is_a_top_level_code(game):
    await bootstrap(game)
    for i in range(5):
        assert (await command(game, i, key(i), shift())).status_code == 200
    err = assert_error(await command(game, 5, key(5), shift()), 422, "daily_limit_exceeded")
    assert err["details"]["reason"] == "daily_limit_exceeded" and err["details"]["version"] == 5
    assert err["details"]["message"] == err["message"] and "snapshot" in err["details"]
    row = await factory_row(game)
    assert row["version"] == 5 and row["state"]["season"]["shiftsLeft"] == 0
    replay = assert_error(await command(game, 5, key(5), shift()), 422, "daily_limit_exceeded")
    assert replay["details"]["replayed"] is True


@pytest.mark.parametrize("cmd,code,reason", [(shift(durationMs=100), "minigame_too_short", "minigame_too_short"),
                                              (shift(score=2), "impossible_score", "score_out_of_range")])
async def test_submit_shift_validation_codes(game, cmd, code, reason):
    """Top-level code + the violated rule in details.reason (the full rule table: tests/test_shift.py)."""
    await bootstrap(game)
    err = assert_error(await command(game, 0, key(1), cmd), 422, code)
    assert err["details"]["reason"] == reason
    assert (await factory_row(game))["version"] == 0


@pytest.mark.parametrize("cmd,reason", [
    ({"type": "tick", "events": [], "from": 1000, "to": 2000}, "tick_removed"),
    ({"type": "undo"}, "history_unavailable"),
    ({"type": "redo"}, "history_unavailable"),
])
async def test_tick_undo_redo_are_command_rejected(game, fake_sim, cmd, reason):
    await bootstrap(game)
    err = assert_error(await command(game, 0, key(1), cmd), 422, "command_rejected")
    assert err["details"]["reason"] == reason and err["details"]["version"] == 0
    assert (await factory_row(game))["version"] == 0
    assert fake_sim.bodies("/v1/command")[0]["cmd"]["type"] == cmd["type"]
    if cmd["type"] == "tick":  # wire name, not the python attribute
        assert fake_sim.bodies("/v1/command")[0]["cmd"] == cmd and "from_" not in fake_sim.bodies("/v1/command")[0]["cmd"]


# ================================================================ POST /api/command: validation

MINIMAL = {
    "ageConfirm": {}, "renameFactory": {"name": "N"}, "setUniversity": {"university": "МГУ"},
    "place": {"kind": "filler", "x": 1, "y": 2, "rot": 0},
    "placeConveyor": {"kind": "belt", "path": [{"x": 1, "y": 1, "dir": "E"}]},
    "rerouteConveyor": {"kind": "belt", "path": [], "remove": []},
    "remove": {"buildingId": "b1"}, "move": {"buildingId": "b1", "x": 1, "y": 1, "rot": 1},
    "rotate": {"buildingId": "b1"}, "configure": {"buildingId": "b1", "config": {"container": "pet15"}},
    "upgrade": {"buildingId": "b1"}, "upgradeConveyor": {"cells": [{"x": 1, "y": 1}]},
    "repair": {"buildingId": "b1"}, "expandHangar": {"side": "N"}, "paint": {"cells": [], "color": None},
    "acceptOrder": {"dockId": "d1", "orderId": "o1"}, "cancelOrder": {"orderId": "o1"},
    "sellMarket": {"boxes": 1, "sku": "kvas_ochakovsky_15"}, "takeLoan": {"amount": 100},
    "repayLoan": {"amount": 100}, "submitShift": {"buildingId": "b1", "result": {
        "difficulty": 1, "durationMs": 5000, "gameId": "g", "metrics": {}, "profession": "lab", "result": "win",
        "score": 0.5}},
    "dialogDone": {"id": "d"}, "factSeen": {"id": "f"}, "undo": {}, "redo": {}, "buildEnd": {}, "offlineSeen": {},
    "tick": {"events": [], "from": 1, "to": 2},
}


def test_minimal_payload_table_covers_the_generated_union():
    assert set(MINIMAL) == set(COMMAND_TYPES)


async def test_every_command_type_validates_and_is_forwarded_as_sent(game, fake_sim):
    await bootstrap(game)
    version = 0
    for i, (t, rest) in enumerate(MINIMAL.items()):
        cmd = {"type": t, **rest}
        resp = await command(game, version, key(i), cmd)
        if t == "buildEnd":  # no-op: answered by the API, never forwarded
            assert resp.status_code == 200 and resp.json()["snapshot"] is None
            continue
        if resp.status_code == 200:
            version = resp.json()["version"]
        else:  # rule rejection by the fake sim (tick/undo/redo/acceptOrder/...), never a validation error
            assert resp.json()["error"]["code"] != "validation_error", (t, resp.text)
        assert fake_sim.bodies("/v1/command")[-1]["cmd"] == cmd, t  # incl. tick.from and paint.color == null
    assert [c["cmd"]["type"] for c in fake_sim.bodies("/v1/command")] == [t for t in MINIMAL if t != "buildEnd"]


async def test_optional_nulls_are_dropped_but_required_nullable_kept(game, fake_sim):
    await bootstrap(game)
    cmd = {**place(), "type": "place", "config": None, "level": None}
    assert (await command(game, 0, key(1), cmd)).status_code == 200
    assert fake_sim.bodies("/v1/command")[-1]["cmd"] == place()
    paint = {"type": "paint", "cells": [{"x": 1, "y": 1}], "color": None}
    await command(game, 1, key(2), paint)
    assert fake_sim.bodies("/v1/command")[-1]["cmd"] == paint


def _body(**over):
    body = {"version": 0, "idempotency_key": "idem-key-0001", "cmd": place()}
    body.update(over)
    return {k: v for k, v in body.items() if v is not ...}


INVALID_BODIES = {
    "unknown_type": _body(cmd={"type": "launchRocket"}),
    "no_type": _body(cmd={"x": 1}),
    "cmd_not_object": _body(cmd="place"),
    "cmd_null": _body(cmd=None),
    "missing_cmd": _body(cmd=...),
    "missing_field": _body(cmd={"type": "place", "kind": "filler", "y": 1, "rot": 0}),
    "bad_enum": _body(cmd={**place(), "kind": "nuclear_plant"}),
    "bad_rot": _body(cmd={**place(), "rot": 7}),
    "extra_field": _body(cmd={**place(), "free_money": 1}),
    "wrong_type": _body(cmd={**place(), "x": "left"}),
    "shift_missing_result": _body(cmd={"type": "submitShift", "buildingId": "b1"}),
    "tick_bad_event": _body(cmd={"type": "tick", "events": [{"k": "nope"}], "from": 1, "to": 2}),
    "key_short": _body(idempotency_key="short"),
    "key_7": _body(idempotency_key="1234567"),
    "key_long": _body(idempotency_key="k" * 65),
    "key_int": _body(idempotency_key=12345678),
    "key_null": _body(idempotency_key=None),
    "key_missing": _body(idempotency_key=...),
    "version_missing": _body(version=...),
    "version_negative": _body(version=-1),
    "version_string": _body(version="0"),
    "version_float": _body(version=0.5),
    "version_bool": _body(version=True),
    "version_null": _body(version=None),
    "buildend_bad_key": _body(cmd={"type": "buildEnd"}, idempotency_key="x"),
}


@pytest.mark.parametrize("name", INVALID_BODIES)
async def test_command_validation_error_never_reaches_the_sim(game, fake_sim, name):
    await bootstrap(game)
    resp = await game.post("/api/command", json=INVALID_BODIES[name], headers=dev(1))
    err = assert_error(resp, 422, "validation_error")
    assert err["details"]["errors"]
    assert fake_sim.bodies("/v1/command") == []
    row = await factory_row(game)
    assert row["version"] == 0 and "meta" not in row["state"]


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b"null", b'"x"'])
async def test_command_malformed_body_is_422(game, raw):
    await bootstrap(game)
    resp = await game.post("/api/command", content=raw, headers={**dev(1), "content-type": "application/json"})
    assert_error(resp, 422, "validation_error")


async def test_key_length_boundaries_are_accepted(game):
    await bootstrap(game)
    assert (await command(game, 0, "k" * 8, place())).status_code == 200
    assert (await command(game, 1, "k" * 64, place(2, 2))).status_code == 200


# ================================================================ buildEnd

async def test_build_end_is_a_pure_read(game, fake_sim):
    await bootstrap(game)
    await command(game, 0, key(1), place())
    before = await factory_row(game)
    calls = len(fake_sim.calls)
    resp = await command(game, 0, key(2), {"type": "buildEnd"})  # stale version: still no 409
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "version": 1, "result": {}, "events": [], "snapshot": None, "replayed": False}
    again = await command(game, 99, key(2), {"type": "buildEnd"})  # same key twice: not a replay either
    assert again.json()["replayed"] is False and again.json()["version"] == 1
    after = await factory_row(game)
    assert after == before  # no write at all (version, state, snapshot, updated_at)
    assert after["state"]["meta"]["idempotency"] == before["state"]["meta"]["idempotency"]
    assert len(fake_sim.calls) == calls  # the sim is not involved


async def test_build_end_before_bootstrap_is_404(game):
    assert_error(await command(game, 0, key(1), {"type": "buildEnd"}), 404, "factory_not_found")


async def test_build_end_does_not_take_the_row_lock_but_a_command_does(game):
    import asyncio
    await bootstrap(game)
    tx = game.db.transaction()
    await tx.start()
    try:
        await game.db.fetchval("SELECT version FROM factories FOR UPDATE")  # somebody holds the row
        resp = await asyncio.wait_for(command(game, 0, key(1), {"type": "buildEnd"}), timeout=3)
        assert resp.status_code == 200 and resp.json()["version"] == 0
        blocked = asyncio.create_task(command(game, 0, key(2), place()))
        await asyncio.sleep(0.4)
        assert not blocked.done()  # a real command waits for the lock
    finally:
        await tx.commit()
    done = await asyncio.wait_for(blocked, timeout=5)
    assert done.status_code == 200 and done.json()["version"] == 1


# ================================================================ POST /api/advance

async def test_advance_shape_catch_up_and_no_version_bump(game, fake_sim, clock):
    await bootstrap(game)
    await command(game, 0, key(1), place())
    clock.t += 30_000
    resp = await advance(game)  # no body at all: defaults
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"version", "serverTime", "advancedMs", "snapshot"}
    assert body["version"] == 1 and body["advancedMs"] == 30_000 and body["serverTime"] == clock.t
    assert body["snapshot"]["factory"]["credits"] == START_CREDITS - PLACE_COST + 30
    sent = fake_sim.bodies("/v1/advance")[0]
    assert sent["now"] == clock.t and sent["capHours"] == 8 and sent["reason"] == "manual"
    row = await factory_row(game)
    assert row["version"] == 1 and row["credits"] == START_CREDITS - PLACE_COST + 30
    assert row["snapshot"] == body["snapshot"]
    assert row["state"]["meta"]["idempotency"][0]["key"] == key(1)  # idempotency log untouched


@pytest.mark.parametrize("payload,cap,reason", [
    ({}, 8, "manual"), ({"cap_hours": 0}, 0, "manual"), ({"cap_hours": 0.5, "reason": "resume"}, 0.5, "resume"),
    ({"cap_hours": 8, "reason": "background"}, 8, "background"), ({"reason": "manual"}, 8, "manual")])
async def test_advance_body_is_forwarded(game, fake_sim, payload, cap, reason):
    await bootstrap(game)
    assert (await advance(game, payload)).status_code == 200
    sent = fake_sim.bodies("/v1/advance")[0]
    assert sent["capHours"] == cap and sent["reason"] == reason


async def test_advance_cap_hours_limits_the_catch_up(game, clock):
    await bootstrap(game)
    clock.t += 3 * 3_600_000
    body = (await advance(game, {"cap_hours": 1})).json()
    assert body["advancedMs"] == 3 * 3_600_000  # whole interval is accounted
    assert body["snapshot"]["factory"]["credits"] == START_CREDITS + 3600  # but output is capped at 1 h


@pytest.mark.parametrize("payload", [
    {"cap_hours": -1}, {"cap_hours": 8.5}, {"cap_hours": 9}, {"cap_hours": "abc"}, {"cap_hours": None},
    {"cap_hours": [8]}, {"reason": "whenever"}, {"reason": 5}, {"reason": None}])
async def test_advance_validation_error(game, fake_sim, payload):
    await bootstrap(game)
    assert_error(await advance(game, payload), 422, "validation_error")
    assert fake_sim.bodies("/v1/advance") == []


@pytest.mark.parametrize("raw", [b"not json", b'{"cap_hours": NaN}', b'{"cap_hours": Infinity}', b"[8]"])
async def test_advance_malformed_body_is_422(game, raw):
    await bootstrap(game)
    resp = await game.post("/api/advance", content=raw, headers={**dev(1), "content-type": "application/json"})
    assert_error(resp, 422, "validation_error")


async def test_advance_before_bootstrap_is_404(game, fake_sim):
    assert_error(await advance(game), 404, "factory_not_found")
    assert fake_sim.paths() == []


async def test_advance_sim_unavailable_is_503_and_writes_nothing(game, fake_sim, clock):
    await bootstrap(game)
    before = await factory_row(game)
    clock.t += 10_000
    fake_sim.down = True
    assert_error(await advance(game), 503, "sim_unavailable")
    assert await factory_row(game) == before


async def test_advance_and_me_never_409_and_do_not_disturb_commands(game, clock):
    """Two tabs on passive sync must not fight: only POST /api/command can produce a 409."""
    await bootstrap(game)  # tab A and tab B are both at version 0
    for _ in range(3):
        clock.t += 1000
        assert (await advance(game)).json()["version"] == 0
        assert (await me(game)).json()["version"] == 0
        assert (await game.get("/api/bootstrap", headers=dev(1))).json()["version"] == 0
    ok = await command(game, 0, key(1), place())  # tab A's command is still fresh
    assert ok.status_code == 200 and ok.json()["version"] == 1
    clock.t += 1000
    assert (await advance(game)).json()["version"] == 1  # and tab B sees the new version without a 409
    assert_error(await command(game, 0, key(2), place(2, 2)), 409, "version_conflict")


# ================================================================ GET /api/factory/me

async def test_factory_me_before_bootstrap_is_404(game, fake_sim):
    assert_error(await me(game), 404, "factory_not_found")
    assert fake_sim.paths() == []


async def test_factory_me_catches_up_under_lock_and_persists(game, fake_sim, clock):
    await bootstrap(game)
    clock.t += 20_000
    resp = await me(game)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"version", "serverTime", "snapshot", "degraded"}
    assert body["degraded"] is False and body["version"] == 0 and body["serverTime"] == clock.t
    assert body["snapshot"]["factory"]["credits"] == START_CREDITS + 20
    sent = fake_sim.bodies("/v1/advance")[0]
    assert sent["capHours"] == 8 and sent["reason"] == "resume" and sent["now"] == clock.t
    row = await factory_row(game)
    assert row["credits"] == START_CREDITS + 20 and row["version"] == 0 and row["snapshot"] == body["snapshot"]


async def test_factory_me_degraded_serves_the_stored_snapshot(game, fake_sim, clock):
    boot = await bootstrap(game)
    await command(game, 0, key(1), place())
    stored = await factory_row(game)
    clock.t += 60_000
    fake_sim.down = True
    resp = await me(game)
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True and body["version"] == 1 and body["serverTime"] == clock.t
    assert body["snapshot"] == stored["snapshot"] != boot["snapshot"]  # the snapshot after the command, no catch-up
    assert body["snapshot"]["factory"]["credits"] == START_CREDITS - PLACE_COST
    assert await factory_row(game) == stored  # nothing written
    fake_sim.down = False
    assert (await me(game)).json()["degraded"] is False  # recovers


@pytest.mark.parametrize("mode", ["status", "garbage"])
async def test_factory_me_degrades_on_bad_sim_answers_too(game, fake_sim, mode):
    await bootstrap(game)
    if mode == "status":
        fake_sim.status = 500
    else:
        fake_sim.garbage = {"nothing": True}
    body = (await me(game)).json()
    assert body["degraded"] is True and body["snapshot"]["factory"]["credits"] == START_CREDITS


async def test_factory_me_degraded_without_stored_snapshot_is_503(game, fake_sim):
    await bootstrap(game)
    await game.db.execute("UPDATE factories SET snapshot = NULL")  # a row from before migration 0002
    fake_sim.down = True
    assert_error(await me(game), 503, "sim_unavailable")


async def test_factory_me_404_even_when_sim_is_down(game, fake_sim):
    fake_sim.down = True
    assert_error(await me(game), 404, "factory_not_found")


# ================================================================ monotonic time

async def test_sim_always_gets_max_of_wall_clock_and_last_tick(game, fake_sim, clock):
    await bootstrap(game)
    future = clock.t + 3_600_000  # a replica with a fast clock wrote this lastTick
    await game.db.execute("UPDATE factories SET state = jsonb_set(state, '{lastTick}', to_jsonb($1::bigint))", future)
    await advance(game)
    await me(game)
    await game.get("/api/bootstrap", headers=dev(1))
    await command(game, 0, key(1), place())
    nows = [c["body"]["now"] for c in fake_sim.calls if c["path"] != "/v1/bootstrap" or c["body"]["state"] is not None]
    assert nows and all(n == future for n in nows)
    # once the wall clock passes lastTick, time follows the wall clock again
    clock.t = future + 5000
    await advance(game)
    assert fake_sim.bodies("/v1/advance")[-1]["now"] == future + 5000


# ================================================================ migration

async def test_migration_0002_adds_nullable_jsonb_snapshot(pg_url, clean_db):
    row = await clean_db.fetchrow("SELECT data_type, is_nullable FROM information_schema.columns "
                                  "WHERE table_name = 'factories' AND column_name = 'snapshot'")
    assert (row["data_type"], row["is_nullable"]) == ("jsonb", "YES")
