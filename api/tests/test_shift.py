"""S3: submitShift server validation (app/shift.py), the `shifts` log, replay safety, undo/redo dispatch, features.undo
and the state size guard. Unit tests of the rules need nothing; the HTTP ones use the throwaway Postgres + Redis and the
in-process fake sim (tests/fake_sim.py). The real sim is covered by tests/test_game_sim.py (opt-in)."""
from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

import pytest

from app import game as game_module
from app.contract.generated import COMMAND_TYPES
from app.shift import (MAX_COUNT, MAX_DURATION_MS, MAX_GAME_ID, MAX_METRICS, MAX_METRIC_KEY, MIN_DURATION_MS, RULES,
                       WIN_MIN_SCORE, validate_shift)
from tests.game_helpers import SHIFT, Clock, advance, bootstrap, command, factory_row, js, key, shift
from tests.helpers import dev
from tests.test_errors import assert_error


@pytest.fixture
def clock(monkeypatch):
    c = Clock(int(time.time() * 1000))
    monkeypatch.setattr("app.game.now_ms", c)
    return c


# ---- (override of the SHIFT base result, violated rule) - the full table, every rule at least once
VIOLATIONS = [
    ({"durationMs": 399}, "minigame_too_short"),
    ({"durationMs": 0}, "minigame_too_short"),
    ({"durationMs": -5}, "minigame_too_short"),
    ({"durationMs": 60_001}, "duration_too_long"),
    ({"durationMs": 10 ** 9}, "duration_too_long"),
    ({"durationMs": float("nan")}, "duration_invalid"),
    ({"durationMs": float("inf")}, "duration_invalid"),
    ({"score": float("nan")}, "score_not_finite"),
    ({"score": float("inf")}, "score_not_finite"),
    ({"score": float("-inf")}, "score_not_finite"),
    ({"score": -0.01}, "score_out_of_range"),
    ({"score": 1.01}, "score_out_of_range"),
    ({"score": 2}, "score_out_of_range"),
    ({"difficulty": 1.01}, "difficulty_out_of_range"),
    ({"difficulty": -0.1}, "difficulty_out_of_range"),
    ({"difficulty": float("nan")}, "difficulty_out_of_range"),
    ({"gameId": ""}, "game_id_invalid"),
    ({"gameId": "g" * (MAX_GAME_ID + 1)}, "game_id_invalid"),
    ({"metrics": {"accuracy": 1.5}}, "accuracy_out_of_range"),
    ({"metrics": {"accuracy": -0.1}}, "accuracy_out_of_range"),
    ({"metrics": {"misses": -1}}, "misses_invalid"),
    ({"metrics": {"misses": 1.5}}, "misses_invalid"),
    ({"metrics": {"misses": MAX_COUNT + 1}}, "misses_invalid"),
    ({"metrics": {"hits": -2}}, "hits_invalid"),
    ({"metrics": {"hits": 2.5}}, "hits_invalid"),
    ({"metrics": {"hits": MAX_COUNT + 1}}, "hits_invalid"),
    ({"metrics": {"reaction_ms": 79}}, "reaction_out_of_range"),
    ({"metrics": {"reaction_ms": 0}}, "reaction_out_of_range"),
    ({"metrics": {"reaction_ms": 10_001}}, "reaction_out_of_range"),
    ({"metrics": {"dodged": float("inf")}}, "metrics_invalid"),
    ({"metrics": {"dodged": float("nan")}}, "metrics_invalid"),
    ({"metrics": {"dodged": 1e7}}, "metrics_invalid"),
    ({"metrics": {f"m{i}": 1 for i in range(MAX_METRICS + 1)}}, "metrics_invalid"),
    ({"metrics": {"k" * (MAX_METRIC_KEY + 1): 1}}, "metrics_invalid"),
    ({"result": "win", "score": 0.0}, "win_score_too_low"),
    ({"result": "win", "score": WIN_MIN_SCORE - 0.01}, "win_score_too_low"),
]

# ---- what the real minigames send (vendor/frontend/web/src/lib/minigames): never rejected
REAL_CLIENTS = [
    ("jam", "adjuster", "win", 0.8, 2400, {"reaction_ms": 1500, "misses": 1, "accuracy": 0.5}),
    ("kipia", "kipia", "win", 0.77, 5100, {"reaction_ms": 640.5, "misses": 0, "accuracy": 1}),
    ("lab-qc", "lab", "win", 0.9, 1900, {"reaction_ms": 1210.3, "accuracy": 1}),
    ("lab-qc", "lab", "fail", 0.15, 1700, {"reaction_ms": 980, "misses": 1}),
    ("forklift", "forklift", "win", 0.6, 3700, {"accuracy": 0, "dodged": 0}),  # survived by timeout, nothing dodged
    ("forklift", "forklift", "win", 0.9, 3700, {"accuracy": 0.8, "dodged": 4}),
    ("kvasovar", "kvasovar", "fail", 0.26, 2200, {"accuracy": 0.6, "fail_reason_len": 24}),
    ("kvasovar", "kvasovar", "win", 0.98, 3600, {"accuracy": 0.9999999999999999}),
    ("filler-tap", "operator", "win", 0.9, 6800, {"reaction_ms": 1100, "misses": 1, "accuracy": 0.75}),
    ("stacker", "storekeeper", "win", 0.94, 8000, {"accuracy": 0.9, "score": 0.94}),
    ("stacker", "storekeeper", "fail", 0.3, 4100, {"accuracy": 0.7, "fail_reason_len": 11}),
]


def result(**over):
    return {**SHIFT, **over}


def shift_cmd(**over):
    return shift(**over)


async def shift_rows(c):
    return [dict(r) for r in await c.db.fetch("SELECT * FROM shifts ORDER BY id")]


async def shifts_left(c, tg: int = 1) -> int:
    return (await factory_row(c, tg))["state"]["season"]["shiftsLeft"]


async def raw_command(c, version: int, idem: str, cmd: dict, tg: int = 1):
    """POST with `json.dumps(allow_nan=True)`: JSON.parse-incompatible NaN / Infinity literals still reach FastAPI."""
    body = json.dumps({"version": version, "idempotency_key": idem, "cmd": cmd})
    return await c.post("/api/command", content=body, headers={**dev(tg), "content-type": "application/json"})


# ================================================================ the rules (pure)

def test_a_plausible_shift_passes():
    assert validate_shift(result()) is None


@pytest.mark.parametrize("over,rule", VIOLATIONS)
def test_validate_shift_rules(over, rule):
    assert validate_shift(result(**over)) == rule


def test_every_rule_is_in_the_table_and_covered():
    assert {rule for _, rule in VIOLATIONS} == set(RULES)  # no untested rule, no unknown rule
    assert {c for c, _ in RULES.values()} == {"minigame_too_short", "impossible_score"}


@pytest.mark.parametrize("over", [
    {"durationMs": MIN_DURATION_MS}, {"durationMs": MAX_DURATION_MS}, {"durationMs": 400.0},
    {"score": 0, "result": "fail"}, {"score": 1}, {"score": 1.0, "result": "win"}, {"difficulty": 0}, {"difficulty": 1},
    {"result": "fail", "score": 0}, {"result": "fail", "score": 0.05},  # a fail may score anything in 0..1
    {"result": "win", "score": WIN_MIN_SCORE}, {"metrics": {}},
    {"metrics": {"accuracy": 0}}, {"metrics": {"accuracy": 1}}, {"metrics": {"accuracy": 1 + 1e-9}},
    {"metrics": {"misses": 0}}, {"metrics": {"misses": MAX_COUNT}}, {"metrics": {"hits": 7}},
    {"metrics": {"reaction_ms": 80}}, {"metrics": {"reaction_ms": 10_000}},
    {"metrics": {f"m{i}": i for i in range(MAX_METRICS)}}, {"gameId": "g" * MAX_GAME_ID},
])
def test_validate_shift_boundaries_are_inclusive(over):
    assert validate_shift(result(**over)) is None


@pytest.mark.parametrize("game_id,prof,res,score,dur,metrics", REAL_CLIENTS)
def test_real_minigame_results_pass(game_id, prof, res, score, dur, metrics):
    assert validate_shift({"gameId": game_id, "profession": prof, "result": res, "score": score, "durationMs": dur,
                           "difficulty": 0.15, "metrics": metrics}) is None


def test_validate_shift_rejects_garbage_shapes():
    assert validate_shift(None) == "metrics_invalid"
    assert validate_shift(result(metrics=[1])) == "metrics_invalid"
    assert validate_shift(result(durationMs=True)) == "duration_invalid"


# ================================================================ every rule over HTTP

@pytest.mark.parametrize("over,rule", VIOLATIONS)
async def test_impossible_shift_is_422_recorded_and_costs_nothing(game, fake_sim, over, rule):
    await bootstrap(game)
    code = RULES[rule][0]
    resp = await raw_command(game, 0, key(1), shift_cmd(**over))
    err = assert_error(resp, 422, code)
    assert err["details"]["reason"] == rule and err["details"]["version"] == 0
    assert err["details"]["message"] == err["message"] == RULES[rule][1]
    assert err["details"]["snapshot"]["season"]["shiftsLeft"] == 5
    # refused BEFORE the sim: no /v1/command, no version bump, no shift consumed, nothing logged
    assert fake_sim.bodies("/v1/command") == []
    row = await factory_row(game)
    assert row["version"] == 0 and row["state"]["season"]["shiftsLeft"] == 5 and row["xp"] == 0
    assert await shift_rows(game) == []
    # recorded in the idempotency log like any rejection
    (entry,) = row["state"]["meta"]["idempotency"]
    assert entry["key"] == key(1) and entry["ok"] is False and entry["code"] == rule and entry["version"] == 0
    # a retry with the same key is the same answer, marked as replayed, still without a sim command
    replay = assert_error(await raw_command(game, 0, key(1), shift_cmd(**over)), 422, code)
    assert replay["details"]["reason"] == rule and replay["details"]["replayed"] is True
    assert fake_sim.bodies("/v1/command") == []
    # ... and the player can try again with a fresh key and an honest result
    ok = await command(game, 0, key(2), shift_cmd())
    assert ok.status_code == 200 and ok.json()["version"] == 1 and await shifts_left(game) == 4
    assert len(await shift_rows(game)) == 1


async def test_rejection_does_not_touch_snapshot_or_denormalised_columns(game, clock):
    await bootstrap(game)
    before = await factory_row(game)
    clock.t += 3_600_000  # an hour of production is waiting, but a refused shift does not catch up
    assert_error(await command(game, 0, key(1), shift_cmd(score=3)), 422, "impossible_score")
    after = await factory_row(game)
    assert after["snapshot"] == before["snapshot"] and after["credits"] == before["credits"]
    assert after["state"]["lastTick"] == before["state"]["lastTick"]


async def test_version_is_checked_before_the_shift_rules(game):
    await bootstrap(game)
    assert (await command(game, 0, key(1), shift_cmd())).status_code == 200
    stale = await command(game, 0, key(2), shift_cmd(score=9))
    assert assert_error(stale, 409, "version_conflict")["details"] == {"current_version": 1, "version": 1}
    assert [e["key"] for e in (await factory_row(game))["state"]["meta"]["idempotency"]] == [key(1)]


async def test_sims_own_checks_stay_as_second_line(game, monkeypatch):
    monkeypatch.setattr(game_module, "validate_shift", lambda r: None)  # pretend the API let it through
    await bootstrap(game)
    short = assert_error(await command(game, 0, key(1), shift_cmd(durationMs=100)), 422, "minigame_too_short")
    assert short["details"]["reason"] == "minigame_too_short"
    bad = assert_error(await command(game, 0, key(2), shift_cmd(score=2)), 422, "impossible_score")
    assert bad["details"]["reason"] == "impossible_score" and bad["details"]["version"] == 0
    assert await shift_rows(game) == []


async def test_pydantic_still_guards_the_shape(game):
    await bootstrap(game)
    for bad in ({"durationMs": "fast"}, {"result": "draw"}, {"profession": "wizard"}, {"extra": 1}):
        assert_error(await command(game, 0, key(1), shift_cmd(**bad)), 422, "validation_error")
    assert (await factory_row(game))["version"] == 0


# ================================================================ accepted shifts and the `shifts` log

async def test_valid_shift_is_accepted_and_logged_in_the_same_transaction(game, fake_sim):
    boot = await bootstrap(game)
    resp = await command(game, 0, key(1), shift_cmd(difficulty=0.4, metrics={"accuracy": 0.9, "misses": 2}))
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1 and body["result"]["rewards"] == {"xp": 10} and body["replayed"] is False
    (row,) = await shift_rows(game)
    assert (row["user_id"], row["season_id"], row["building_id"]) == (1, boot["season"]["id"], "b1")
    assert (row["profession"], row["game_id"], row["result"]) == ("lab", "g1", "win")
    assert row["score"] == pytest.approx(0.8) and row["duration_ms"] == 5000 and row["difficulty"] == pytest.approx(0.4)
    assert js(row["metrics"]) == {"accuracy": 0.9, "misses": 2}
    assert row["boost_mult"] == pytest.approx(1.24)  # building boost read from the new state
    assert row["created_at"] is not None
    assert len(fake_sim.bodies("/v1/command")) == 1
    state = (await factory_row(game))["state"]
    assert state["season"]["shiftsLeft"] == 4 and state["xp"] == 10


async def test_failed_minigame_is_logged_too_with_its_boost(game):
    await bootstrap(game)
    assert (await command(game, 0, key(1), shift_cmd(result="fail", score=0.4))).status_code == 200
    (row,) = await shift_rows(game)
    assert row["result"] == "fail" and row["boost_mult"] == pytest.approx(1.06)  # the mock halves the score on a fail


async def test_boost_mult_is_null_when_the_state_has_no_such_building(game, monkeypatch):
    await bootstrap(game)
    monkeypatch.setattr(game_module, "building_boost", lambda state, bid: None)
    assert (await command(game, 0, key(1), shift_cmd())).status_code == 200
    assert (await shift_rows(game))[0]["boost_mult"] is None


@pytest.mark.parametrize("game_id,prof,res,score,dur,metrics", REAL_CLIENTS)
async def test_real_minigame_results_are_accepted_over_http(game, game_id, prof, res, score, dur, metrics):
    await bootstrap(game)
    cmd = {"type": "submitShift", "buildingId": "b1", "result": {
        "gameId": game_id, "profession": prof, "result": res, "score": score, "durationMs": dur,
        "difficulty": 0.23, "metrics": metrics}}
    assert (await command(game, 0, key(1), cmd)).status_code == 200
    (row,) = await shift_rows(game)
    assert row["game_id"] == game_id and js(row["metrics"]) == metrics


@pytest.mark.parametrize("flush_redis_first", [False, True], ids=["redis-fast-path", "db-log-path"])
async def test_replay_of_submit_shift_gives_no_double_reward(game, flush_redis_first):
    await bootstrap(game)
    first = (await command(game, 0, key(1), shift_cmd())).json()
    before = await factory_row(game)
    if flush_redis_first:
        await game.redis.flushall()  # the replay must come from state.meta.idempotency, not from the cache
    for stale_version in (0, 1):  # the retry carries the old version, or the new one
        replay = (await command(game, stale_version, key(1), shift_cmd())).json()
        assert replay["replayed"] is True and replay["version"] == 1
        assert replay["result"] == first["result"] and replay["snapshot"]["season"]["shiftsLeft"] == 4
    after = await factory_row(game)
    assert (after["version"], after["xp"], after["state"]["xp"]) == (1, 10, 10)  # one xp reward
    assert after["state"]["season"]["shiftsLeft"] == 4  # one shift consumed
    assert after["state"]["sim"]["layout"]["buildings"] == before["state"]["sim"]["layout"]["buildings"]  # one boost
    assert len(await shift_rows(game)) == 1


async def test_sixth_shift_of_the_day_is_daily_limit_exceeded_and_not_logged(game):
    await bootstrap(game)
    for i in range(5):
        assert (await command(game, i, key(i), shift_cmd())).status_code == 200
    err = assert_error(await command(game, 5, key(5), shift_cmd()), 422, "daily_limit_exceeded")
    assert err["details"]["reason"] == "daily_limit_exceeded" and err["details"]["snapshot"]["season"]["shiftsLeft"] == 0
    assert len(await shift_rows(game)) == 5
    row = await factory_row(game)
    assert row["version"] == 5 and row["state"]["season"]["shiftsLeft"] == 0  # the counter is state, not Redis


async def test_rejected_shifts_do_not_eat_the_daily_limit(game):
    await bootstrap(game)
    plan = [({"durationMs": 50}, False), ({"score": 7}, False), ({}, True), ({"durationMs": 90_000}, False), ({}, True),
            ({"metrics": {"misses": -1}}, False), ({}, True), ({"result": "win", "score": 0}, False), ({}, True), ({}, True)]
    n = 0  # accepted so far = current version
    for i, (over, accepted) in enumerate(plan):
        resp = await command(game, n, key(i), shift_cmd(**over))
        assert resp.status_code == (200 if accepted else 422), (over, resp.text)
        n += accepted
    # five honest shifts went through whatever the garbage in between
    assert n == 5 and await shifts_left(game) == 0 and len(await shift_rows(game)) == 5
    assert_error(await command(game, 5, key("x"), shift_cmd()), 422, "daily_limit_exceeded")
    assert len(await shift_rows(game)) == 5


async def test_the_shift_row_and_the_state_commit_or_roll_back_together(game, monkeypatch):
    await bootstrap(game)

    async def boom(*a, **k):
        raise RuntimeError("shifts insert failed (test)")

    monkeypatch.setattr(game_module.GameService, "_log_shift", boom)
    assert (await command(game, 0, key(1), shift_cmd())).status_code == 500
    row = await factory_row(game)
    assert row["version"] == 0 and row["state"]["season"]["shiftsLeft"] == 5 and row["xp"] == 0
    assert "meta" not in row["state"] or row["state"]["meta"].get("idempotency") in (None, [])
    assert await shift_rows(game) == []


async def test_other_commands_write_no_shift_rows(game):
    await bootstrap(game)
    assert (await command(game, 0, key(1), {"type": "renameFactory", "name": "Z"})).status_code == 200
    assert await shift_rows(game) == []


async def test_migration_0003_creates_the_shifts_table(pg_url, clean_db):
    cols = {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in await clean_db.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name = 'shifts'")}
    assert cols == {
        "id": ("bigint", "NO"), "user_id": ("bigint", "NO"), "season_id": ("integer", "NO"),
        "building_id": ("text", "NO"), "profession": ("text", "NO"), "game_id": ("text", "NO"),
        "result": ("text", "NO"), "score": ("real", "NO"), "duration_ms": ("integer", "NO"),
        "difficulty": ("real", "YES"), "metrics": ("jsonb", "NO"), "boost_mult": ("real", "YES"),
        "created_at": ("timestamp with time zone", "NO")}
    index = await clean_db.fetchval("SELECT indexdef FROM pg_indexes WHERE tablename = 'shifts' AND indexname = 'ix_shifts_user_created'")
    assert index and "(user_id, created_at)" in index


# ================================================================ undo / redo dispatch and features.undo

@pytest.mark.parametrize("t", ["undo", "redo"])
async def test_undo_redo_are_history_unavailable_while_the_state_has_no_history(game, t):
    boot = await bootstrap(game)
    assert boot["features"]["undo"] is False
    err = assert_error(await command(game, 0, key(1), {"type": t}), 422, "command_rejected")
    assert err["details"]["reason"] == "history_unavailable" and err["details"]["version"] == 0
    assert (await factory_row(game))["version"] == 0
    replay = assert_error(await command(game, 0, key(1), {"type": t}), 422, "command_rejected")
    assert replay["details"]["replayed"] is True


@pytest.mark.parametrize("t", ["undo", "redo"])
async def test_undo_redo_are_ordinary_effect_commands_once_the_sim_serves_them(game, fake_sim, t):
    """The dispatch hook: nothing in the API special-cases undo/redo, so the day S1U lands they just work."""
    fake_sim.history_in_state = True
    boot = await bootstrap(game)
    assert boot["features"]["undo"] is True
    resp = await command(game, 0, key(1), {"type": t})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1 and body["result"]["toast"] and body["replayed"] is False  # version bumped
    assert (await factory_row(game))["state"][t] == 1
    again = (await command(game, 0, key(1), {"type": t})).json()  # idempotent like everything else
    assert again["replayed"] is True and (await factory_row(game))["state"][t] == 1
    assert (await command(game, 1, key(2), {"type": t})).json()["version"] == 2  # and conflicts like everything else
    assert_error(await command(game, 1, key(3), {"type": t}), 409, "version_conflict")


def test_undo_redo_are_in_the_command_union():
    assert {"undo", "redo"} <= set(COMMAND_TYPES)


async def test_features_undo_follows_the_sim_flag_and_is_cached_for_60_s(game, fake_sim, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.sim_client.time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    feats = (await bootstrap(game))["features"]
    assert feats["undo"] is False and feats["serverOrderEta"] is True
    fake_sim.history_in_state = True  # S1U deployed ...
    now[0] += 59
    assert (await bootstrap(game))["features"]["undo"] is False  # ... but the verdict is cached for 60 s
    now[0] += 2
    assert (await bootstrap(game))["features"]["undo"] is True
    fake_sim.history_in_state = False
    assert (await bootstrap(game))["features"]["undo"] is True and fake_sim.health_calls == 2
    now[0] += 61
    assert (await bootstrap(game))["features"]["undo"] is False and fake_sim.health_calls == 3


async def test_features_undo_is_not_fetched_by_other_endpoints(game, fake_sim):
    await bootstrap(game)
    calls = fake_sim.health_calls
    await advance(game)
    await command(game, 0, key(1), shift_cmd())
    assert fake_sim.health_calls == calls


# ================================================================ state size guard

async def test_state_over_150_kb_logs_a_state_bytes_warning(game, fake_sim, caplog):
    await bootstrap(game)
    with caplog.at_level(logging.WARNING, logger="mosprom.game"):
        assert (await command(game, 0, key(1), shift_cmd())).status_code == 200
    assert not [r for r in caplog.records if hasattr(r, "fields") and "state_bytes" in r.fields]  # small state: silent

    fake_sim.bloat = 160 * 1024
    with caplog.at_level(logging.WARNING, logger="mosprom.game"):
        assert (await command(game, 1, key(2), shift_cmd())).status_code == 200
    (rec,) = [r for r in caplog.records if hasattr(r, "fields") and "state_bytes" in r.fields]
    assert rec.levelno == logging.WARNING
    assert rec.fields["state_bytes"] > 150 * 1024 and rec.fields["limit_bytes"] == 150 * 1024 and rec.fields["user_id"] == 1
    assert (await factory_row(game))["version"] == 2  # a warning, never a failure
