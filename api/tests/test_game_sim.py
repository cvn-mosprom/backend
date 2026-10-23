"""Opt-in end-to-end tests of the game endpoints against a REAL sim service.

    SIM_URL=http://127.0.0.1:58080 .venv/Scripts/python -m pytest tests/test_game_sim.py

Skipped unless SIM_URL is set (the autouse env cleaner in conftest removes it per test, so it is captured at import).
Needs the throwaway Postgres / Redis like every other DB test. Sim rejection codes (`result.code`) are asserted
strictly only when the sim under test emits them (detected through `undo`), so the module also runs against an
older sim build.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from app.contract.generated import GameSnapshot
from app.sim_client import SimClient
from tests.game_helpers import advance, bootstrap, command, factory_row, key, me, shift
from tests.helpers import TEST_DB_URL, TEST_REDIS_URL, dev
from tests.test_errors import assert_error

SIM_URL = os.environ.get("SIM_URL", "").strip()
pytestmark = pytest.mark.skipif(not SIM_URL, reason="set SIM_URL (e.g. http://127.0.0.1:58080) to run against a real sim")

BREW_TANK = {"type": "place", "kind": "brew_tank", "x": 6, "y": 3, "rot": 0}  # kvasovar station, unlocked at level 1
KVASOVAR_SHIFT = shift(profession="kvasovar")


@pytest_asyncio.fixture
async def real(make_client, clean_db, clean_redis):
    client = await make_client(sim=SimClient(SIM_URL, 5.0), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                               rate_limit_enabled=False)
    client.db = clean_db  # type: ignore[attr-defined]
    client.redis = clean_redis  # type: ignore[attr-defined]
    return client


def credits_of(body: dict) -> float:
    return body["snapshot"]["factory"]["credits"]


async def test_bootstrap_reload_keeps_the_factory(real):
    first = await bootstrap(real)
    GameSnapshot.model_validate(first["snapshot"])  # the sim snapshot matches the generated contract
    assert first["version"] == 0 and first["snapshot"]["factory"]["layout"]["buildings"] == []
    row = await factory_row(real)
    assert row["credits"] == credits_of(first) and row["snapshot"] == first["snapshot"]
    assert row["state"]["v"] == 1 and row["state"]["player"]["id"]

    assert (await command(real, 0, key(1), BREW_TANK)).status_code == 200
    reloaded = await bootstrap(real)  # "reload loses nothing"
    assert reloaded["version"] == 1
    assert [b["kind"] for b in reloaded["snapshot"]["factory"]["layout"]["buildings"]] == ["brew_tank"]


async def test_effect_command_replay_and_conflict(real):
    boot = await bootstrap(real)
    ok = await command(real, 0, key(1), BREW_TANK)
    assert ok.status_code == 200
    body = ok.json()
    assert body["version"] == 1 and body["replayed"] is False and body["result"]["toast"]
    GameSnapshot.model_validate(body["snapshot"])
    spent = credits_of(boot) - credits_of(body)
    assert spent > 0

    replay = (await command(real, 0, key(1), BREW_TANK)).json()  # same key, stale version
    assert replay["replayed"] is True and replay["version"] == 1
    assert credits_of(boot) - credits_of(replay) == spent  # not charged twice
    assert (await factory_row(real))["state"]["meta"]["idempotency"][0]["key"] == key(1)  # sim kept meta intact

    conflict = await command(real, 0, key(2), {"type": "dialogDone", "id": "intro"})
    assert assert_error(conflict, 409, "version_conflict")["details"] == {"current_version": 1}
    # resync -> retry works
    synced = (await me(real)).json()
    assert synced["version"] == 1 and synced["degraded"] is False
    assert (await command(real, synced["version"], key(2), {"type": "dialogDone", "id": "intro"})).json()["version"] == 2
    # the sim round-trips meta through every call
    keys = [e["key"] for e in (await factory_row(real))["state"]["meta"]["idempotency"]]
    assert keys == [key(1), key(2)]


async def test_rejected_by_game_rules_is_422_and_recorded(real):
    await bootstrap(real)
    err = assert_error(await command(real, 0, key(1), {"type": "acceptOrder", "dockId": "x", "orderId": "y"}),
                       422, "command_rejected")
    assert err["details"]["version"] == 0 and err["details"]["message"] and err["details"]["reason"]
    GameSnapshot.model_validate(err["details"]["snapshot"])
    assert (await factory_row(real))["version"] == 0
    again = assert_error(await command(real, 0, key(1), {"type": "acceptOrder", "dockId": "x", "orderId": "y"}),
                         422, "command_rejected")
    assert again["details"]["replayed"] is True


async def test_tick_undo_redo_rejections(real):
    await bootstrap(real)
    undo = assert_error(await command(real, 0, key(1), {"type": "undo"}), 422, "command_rejected")
    tick = assert_error(await command(real, 0, key(2), {"type": "tick", "events": [], "from": 1, "to": 2}),
                        422, "command_rejected")
    redo = assert_error(await command(real, 0, key(3), {"type": "redo"}), 422, "command_rejected")
    for e in (undo, tick, redo):
        assert e["details"]["version"] == 0
    if undo["details"]["reason"] != "rejected":  # a sim build with rejection codes
        assert undo["details"]["reason"] == redo["details"]["reason"] == "history_unavailable"
        assert tick["details"]["reason"] == "tick_removed"
    assert (await factory_row(real))["version"] == 0


async def test_sixth_shift_of_the_day_is_rejected_by_the_server(real):
    await bootstrap(real)
    assert (await command(real, 0, key("p"), BREW_TANK)).status_code == 200
    for i in range(5):
        r = await command(real, 1 + i, key(i), KVASOVAR_SHIFT)
        assert r.status_code == 200, r.text
        assert r.json()["snapshot"]["season"]["shiftsLeft"] == 4 - i
    sixth = await command(real, 6, key(5), KVASOVAR_SHIFT)
    has_codes = (await command(real, 6, key(98), {"type": "undo"})).json()["error"]["details"]["reason"] != "rejected"
    err = assert_error(sixth, 422, "daily_limit_exceeded" if has_codes else "command_rejected")
    assert err["details"]["version"] == 6 and err["details"]["snapshot"]["season"]["shiftsLeft"] == 0
    row = await factory_row(real)
    assert row["version"] == 6 and row["state"]["season"]["shiftsLeft"] == 0  # the counter lives in the state
    replay = await command(real, 6, key(5), KVASOVAR_SHIFT)  # a retry of the rejected shift is the same answer
    assert replay.status_code == 422 and replay.json()["error"]["details"]["replayed"] is True


async def test_validation_errors_do_not_reach_the_sim(real):
    await bootstrap(real)
    bad = await real.post("/api/command", json={"version": 0, "idempotency_key": "idem-key-0001",
                                                "cmd": {"type": "launchRocket"}}, headers=dev(1))
    assert_error(bad, 422, "validation_error")
    short = await command(real, 0, "short", BREW_TANK)
    assert_error(short, 422, "validation_error")
    assert (await factory_row(real))["version"] == 0


async def test_advance_and_me_with_the_real_sim(real):
    await bootstrap(real)
    adv = await advance(real, {"cap_hours": 1, "reason": "resume"})
    assert adv.status_code == 200
    body = adv.json()
    assert body["version"] == 0 and body["advancedMs"] >= 0
    GameSnapshot.model_validate(body["snapshot"])
    mine = (await me(real)).json()
    assert mine["degraded"] is False and mine["version"] == 0
    GameSnapshot.model_validate(mine["snapshot"])
    build_end = await command(real, 0, key(1), {"type": "buildEnd"})
    assert build_end.json()["snapshot"] is None and (await factory_row(real))["version"] == 0


async def test_sim_down_degrades_factory_me_and_503s_the_rest(make_client, clean_db, clean_redis):
    dead = SimClient("http://127.0.0.1:1", 0.5)  # nothing listens there
    live = SimClient(SIM_URL, 5.0)
    client = await make_client(sim=live, database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, rate_limit_enabled=False)
    client.db = clean_db  # type: ignore[attr-defined]
    stored = await bootstrap(client)
    client.app.state.sim = dead
    degraded = (await me(client)).json()
    assert degraded["degraded"] is True and degraded["snapshot"] == stored["snapshot"] and degraded["version"] == 0
    assert_error(await advance(client), 503, "sim_unavailable")
    assert_error(await command(client, 0, key(1), BREW_TANK), 503, "sim_unavailable")
    assert_error(await client.get("/api/bootstrap", headers=dev(1)), 503, "sim_unavailable")
    await dead.aclose()
