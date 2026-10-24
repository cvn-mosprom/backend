"""Opt-in end-to-end tests of the game endpoints against a REAL sim service.

    SIM_URL=http://127.0.0.1:58080 .venv/Scripts/python -m pytest tests/test_game_sim.py

Skipped unless SIM_URL is set (the autouse env cleaner in conftest removes it per test, so it is captured at import).
Needs the throwaway Postgres / Redis like every other DB test. The sim under test is the S12 one (vendored
`lib/core`): state schema v2, undo/redo served from `state.history`, rejection codes from the core.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import pytest_asyncio

from app.contract.generated import GameSnapshot
from app.sim_client import SimClient
from tests.game_helpers import Clock, advance, bootstrap, command, factory_row, key, me, shift
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
    assert row["state"]["v"] == 2 and row["state"]["player"]["id"]

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
    assert assert_error(conflict, 409, "version_conflict")["details"]["current_version"] == 1
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


async def test_tick_is_a_validation_error_and_undo_on_an_empty_history_is_a_rule_rejection(real):
    await bootstrap(real)
    tick = assert_error(await command(real, 0, key(2), {"type": "tick", "events": [], "from": 1, "to": 2}),
                        422, "validation_error")
    assert tick["details"]["errors"]
    for i, t in enumerate(("undo", "redo")):
        err = assert_error(await command(real, 0, key(3 + i), {"type": t}), 422, "command_rejected")
        assert err["details"]["version"] == 0 and err["details"]["reason"] == "rejected"
        assert err["details"]["message"] in ("Нечего отменять", "Нечего повторять")
    assert (await factory_row(real))["version"] == 0


async def test_undo_redo_work_over_http_and_survive_a_re_request(real):
    """The history is part of factories.state: undo/redo go through the ordinary command path (version + 1, idempotent),
    and a fresh bootstrap / factory/me in between loses nothing."""
    boot = await bootstrap(real)
    assert boot["features"]["undo"] is True  # /health historyInState
    assert (await command(real, 0, key(1), BREW_TANK)).status_code == 200
    again = await bootstrap(real)
    assert again["snapshot"]["undoSteps"] == 1 and again["snapshot"]["undoLabel"]
    assert len((await factory_row(real))["state"]["history"]["undo"]) == 1

    undo = await command(real, 1, key(2), {"type": "undo"})
    assert undo.status_code == 200, undo.text
    body = undo.json()
    assert body["version"] == 2 and body["snapshot"]["factory"]["layout"]["buildings"] == []
    assert (body["snapshot"]["undoSteps"], body["snapshot"]["redoSteps"]) == (0, 1)
    assert (await me(real)).json()["snapshot"]["redoSteps"] == 1  # a re-request keeps the history
    replay = (await command(real, 1, key(2), {"type": "undo"})).json()  # same key: not undone twice
    assert replay["replayed"] is True and replay["version"] == 2

    redo = await command(real, 2, key(3), {"type": "redo"})
    assert redo.status_code == 200, redo.text
    assert [b["kind"] for b in redo.json()["snapshot"]["factory"]["layout"]["buildings"]] == ["brew_tank"]
    assert (await factory_row(real))["version"] == 3


TUTORIAL_LINE = json.loads(Path(__file__).with_name("tutorial_line.json").read_text("utf-8"))  # content/tutorial.ts quest line


async def test_eta_at_appears_on_loading_orders_and_follows_a_rebuild(real):
    """Server schedule (Order.etaAt): present on a loading order, gone while the line is cut, back with undo."""
    await bootstrap(real)
    version = 0
    for i, cmd in enumerate(TUTORIAL_LINE):
        resp = await command(real, version, key(100 + i), cmd)
        assert resp.status_code == 200, (cmd, resp.text)
        version = resp.json()["version"]
    snap = (await me(real)).json()["snapshot"]
    order = next(o for o in snap["orders"] if o["status"] == "available")
    dock = next(b for b in snap["factory"]["layout"]["buildings"] if b["kind"] == "dock")
    accepted = await command(real, version, key(200), {"type": "acceptOrder", "orderId": order["id"], "dockId": dock["id"]})
    assert accepted.status_code == 200, accepted.text
    version = accepted.json()["version"]
    loading = next(o for o in accepted.json()["snapshot"]["orders"] if o["status"] == "loading")
    assert isinstance(loading["etaAt"], (int, float)) and loading["etaAt"] >= accepted.json()["snapshot"]["serverTime"]
    belt = next(c for c in accepted.json()["snapshot"]["factory"]["layout"]["conveyors"] if c["kind"] == "belt")
    cut = await command(real, version, key(201), {"type": "remove", "cells": [{"x": belt["x"], "y": belt["y"]}], "kind": "belt"})
    assert cut.status_code == 200, cut.text
    assert next(o for o in cut.json()["snapshot"]["orders"] if o["id"] == loading["id"]).get("etaAt") is None
    back = await command(real, cut.json()["version"], key(202), {"type": "undo"})
    assert back.status_code == 200, back.text
    restored = next(o for o in back.json()["snapshot"]["orders"] if o["id"] == loading["id"])
    assert abs(restored["etaAt"] - loading["etaAt"]) < 5 * 60_000


async def test_sixth_shift_of_the_day_is_rejected_by_the_server(real):
    await bootstrap(real)
    assert (await command(real, 0, key("p"), BREW_TANK)).status_code == 200
    for i in range(5):
        r = await command(real, 1 + i, key(i), KVASOVAR_SHIFT)
        assert r.status_code == 200, r.text
        assert r.json()["snapshot"]["season"]["shiftsLeft"] == 4 - i
    sixth = await command(real, 6, key(5), KVASOVAR_SHIFT)
    err = assert_error(sixth, 422, "daily_limit_exceeded")
    assert err["details"]["version"] == 6 and err["details"]["snapshot"]["season"]["shiftsLeft"] == 0
    row = await factory_row(real)
    assert row["version"] == 6 and row["state"]["shiftsLeft"] == 0  # the counter lives in the state
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


# ================================================================ state size audit (spec: state <= 150 KB)

BIG_LAYOUT = Path(__file__).with_name("big_demo_layout.json")  # vendor lib/sim/templates.ts bigDemoLayout(): 48 buildings
STATE_LIMIT_BYTES = 150 * 1024
LEVEL5_XP = 1400  # content/levels.ts LEVEL_XP[5]; MockGameApi.loadDemo('big') sets level 5 for the big layout


def session_script(layout: dict) -> list[dict]:
    """~120 commands a busy player might issue on the big layout: build edits (the ones the undo history records),
    orders, market, loan, shifts, dialogs, plus undo/redo. Many are refused by the rules - that is fine, they are
    recorded in the idempotency log, which is part of the state too."""
    ids = [b["id"] for b in layout["buildings"]]
    docks = [b["id"] for b in layout["buildings"] if b["kind"] in ("dock", "ramp", "shipping")] or ids[-1:]
    cmds: list[dict] = []
    for i in range(24):
        bid = ids[(i * 5) % len(ids)]
        cmds += [
            {"type": "rotate", "buildingId": bid},
            {"type": "upgrade", "buildingId": bid},
            {"type": "paint", "cells": [{"x": i % 20, "y": 0}, {"x": (i + 1) % 20, "y": 0}], "color": ("yellow", "green", "red")[i % 3]},
            {"type": "move", "buildingId": bid, "x": layout["buildings"][(i * 5) % len(ids)]["x"], "y": layout["buildings"][(i * 5) % len(ids)]["y"], "rot": (i + 1) % 4},
            {"type": "undo"} if i % 4 == 3 else {"type": "repair", "buildingId": bid},
            {"type": "sellMarket", "sku": "kvas_ochakovsky_15", "boxes": 1},
        ]
    cmds += [{"type": "takeLoan", "amount": 500}, {"type": "repayLoan", "amount": 100}, {"type": "expandHangar", "side": "N"},
             {"type": "acceptOrder", "dockId": docks[0], "orderId": "none"}, {"type": "renameFactory", "name": "Большой цех"},
             {"type": "setUniversity", "university": "МГУ"}, {"type": "factSeen", "id": "f1"}, {"type": "redo"}]
    cmds += [{"type": "submitShift", "buildingId": ids[i], "result": {
        "gameId": "e2e", "profession": "operator", "result": "win", "score": 0.7, "durationMs": 4000, "difficulty": 0.5,
        "metrics": {"accuracy": 0.8, "misses": 1, "reaction_ms": 900}}} for i in range(6)]
    return cmds


async def test_state_stays_under_150_kb_after_a_long_session_on_the_big_layout(real, monkeypatch):
    clock = Clock(int(time.time() * 1000))
    monkeypatch.setattr("app.game.now_ms", clock)
    await bootstrap(real)
    layout = json.loads(BIG_LAYOUT.read_text("utf-8"))
    state = (await factory_row(real))["state"]
    state["sim"]["layout"] = layout  # MockGameApi.loadDemo('big'): the layout + level 5, plus money for the session
    state["sim"]["credits"] = 50_000_000
    state["sim"]["xp"] = LEVEL5_XP
    state["level"] = 5
    for q in ("q_intro", "q_brew", "q_bottles", "q_filler", "q_line"):
        if q not in state["dialogs"]["done"]:
            state["dialogs"]["done"].append(q)
    await real.db.execute("UPDATE factories SET state = $1::jsonb", json.dumps(state))

    version, accepted, rejected, peak = 0, 0, 0, 0
    for i, cmd in enumerate(session_script(layout)):
        clock.t += 20 * 60_000  # server time passes between the actions
        resp = await command(real, version, f"audit-{i:05d}", cmd)
        if resp.status_code == 200:
            version, accepted = resp.json()["version"], accepted + 1
        else:
            assert resp.status_code == 422, resp.text
            version, rejected = resp.json()["error"]["details"]["version"], rejected + 1
        if i % 20 == 0:
            peak = max(peak, len(json.dumps((await factory_row(real))["state"])))
    assert accepted >= 25, f"the session must actually change the factory ({accepted} accepted, {rejected} rejected)"
    final = (await factory_row(real))["state"]
    size = len(json.dumps(final))
    print(f"state_bytes={size} peak_sampled={max(peak, size)} accepted={accepted} rejected={rejected} "
          f"buildings={len(final['sim']['layout']['buildings'])} idempotency={len(final['meta']['idempotency'])}")
    assert size < STATE_LIMIT_BYTES, (f"state is {size} bytes (> {STATE_LIMIT_BYTES}) after {accepted} accepted / "
                                      f"{rejected} rejected commands")


async def test_server_season_alignment_against_the_real_core(real):
    """The core would roll a long-closed season to id + 1 by itself; the API pins the state to the server's season
    first, so the sim lands on the server season (id 3) with the core's reset semantics and the layout kept."""
    await real.db.execute("INSERT INTO seasons (starts_at, ends_at, days, closed) VALUES "
                          "(now() - interval '30 days', now() - interval '20 days', 7, true),"
                          "(now() - interval '19 days', now() - interval '10 days', 7, true),"
                          "(now() - interval '1 day', now() + interval '6 days', 7, false)")
    boot = await bootstrap(real)
    GameSnapshot.model_validate(boot["snapshot"])
    sid, season = 3, boot["season"]
    assert season["id"] == sid and boot["snapshot"]["season"]["id"] == sid and boot["snapshot"]["season"]["closed"] is False
    assert boot["snapshot"]["season"]["endsAt"] == season["endsAt"]
    assert (await command(real, 0, key(1), BREW_TANK)).status_code == 200
    await real.db.execute(
        "UPDATE factories SET season_id = 1, state = jsonb_set(jsonb_set(jsonb_set(jsonb_set(state, '{sim,stars}', '123'), "
        "'{sim,shipped}', '{\"kvas\": 5}'), '{sim,rejected}', '2'), '{season}', "
        "(state->'season') || '{\"id\": 1, \"startsAt\": 1000, \"endsAt\": 2000}'::jsonb)")
    again = await bootstrap(real)  # stale state: season 1, long closed
    snap = again["snapshot"]
    GameSnapshot.model_validate(snap)
    assert again["season"]["id"] == sid and snap["season"]["id"] == sid and snap["season"]["closed"] is False
    assert (snap["factory"]["stars"], snap["factory"]["shipped"], snap["factory"]["rejected"]) == (0, {}, 0)
    assert [b["kind"] for b in snap["factory"]["layout"]["buildings"]] == ["brew_tank"]  # factory kept
    row = await factory_row(real)
    assert row["season_id"] == sid and row["state"]["season"]["id"] == sid and row["version"] == 1
    assert (await bootstrap(real))["snapshot"]["season"]["id"] == sid  # stable


async def test_age_confirm_reaches_the_user_row_with_the_real_sim(real):
    boot = await bootstrap(real, 7, "Age")
    assert not boot["snapshot"]["player"].get("ageConfirmed")
    assert (await real.db.fetchval("SELECT age_confirmed FROM users WHERE tg_id = 7")) is False
    res = await command(real, boot["version"], key(1), {"type": "ageConfirm"}, tg=7)
    assert res.status_code == 200, res.text
    assert res.json()["snapshot"]["player"]["ageConfirmed"] is True
    row = await real.db.fetchrow("SELECT age_confirmed, age_confirmed_at FROM users WHERE tg_id = 7")
    assert row["age_confirmed"] is True and row["age_confirmed_at"] is not None


# ================================================================ old (v1) factories load without loss

async def test_a_v1_state_written_by_the_old_sim_is_migrated_in_place_on_the_first_request(real):
    """factories.state of the pre-core sim (schema v1: shifts inside `season`, mock clock, no history) goes through
    migrateState on the first load; layout, money, meta and the unseen report are kept, the shift counter moves."""
    boot = await bootstrap(real)
    state = (await factory_row(real))["state"]
    legacy = {k: v for k, v in state.items() if k not in ("shiftsLeft", "shiftsDay", "history")}
    legacy["v"] = 1
    legacy["season"] = {**state["season"], "shiftsLeft": 3, "shiftsDay": state["shiftsDay"]}
    legacy["clock"] = {"real": 1, "server": 1, "scale": 1}
    legacy["lastSeenReal"] = 1
    legacy["prodAt"] = state["lastTick"]
    legacy["sim"]["layout"]["buildings"] = [{"id": "b1", "kind": "brew_tank", "x": 6, "y": 3, "rot": 0, "level": 1, "wear": 0}]
    await real.db.execute("UPDATE factories SET state = $1::jsonb", json.dumps(legacy))
    got = (await me(real)).json()
    assert got["snapshot"]["season"]["shiftsLeft"] == 3
    assert [b["id"] for b in got["snapshot"]["factory"]["layout"]["buildings"]] == ["b1"]
    row = (await factory_row(real))["state"]
    assert row["v"] == 2 and row["shiftsLeft"] == 3 and "shiftsLeft" not in row["season"]
    assert row["history"] == {"undo": [], "redo": []} and "clock" not in row and "prodAt" not in row
    assert boot["version"] == (await factory_row(real))["version"] == 0  # a migration is not a command: no version bump
