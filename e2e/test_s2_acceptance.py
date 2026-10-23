"""S2 acceptance ("Готово когда", docs/spec-rev4.txt block 8) against the running compose stack.

(a) tutorial passes on the server   (b) reload keeps the factory   (c) two tabs on passive sync do not fight
(d) stale action -> 409 -> resync -> retry works   (e) replay with the same key charges once
(f) 6th shift of the day is rejected   (g) buildEnd does not touch the version   (h) undo -> history_unavailable
"""

import os
import threading
import time
import uuid

import tutorial as T
from conftest import Api, building, err, layout_of, order_of

START_CREDITS = 3000  # content/balance.ts
SHIFTS_PER_DAY = 5
KVAS = "kvas_ochakovsky_15"
ORDER_WAIT_S = float(os.environ.get("E2E_ORDER_WAIT_S", "120"))


def shift_result(profession: str = "kvasovar", **over):
    """ShiftResult (lib/minigames/types.ts) that passes the sim's plausibility check (>= 400 ms, score 0..1)."""
    base = {
        "gameId": "e2e_stub",
        "profession": profession,
        "result": "win",
        "score": 0.8,
        "durationMs": 6000,
        "difficulty": 0.5,
        "metrics": {"accuracy": 0.8, "misses": 0},
    }
    return base | over


# --------------------------------------------------------------------------- (a)
def test_a_tutorial_passes_on_server(api: Api):
    boot = api.bootstrap()
    assert isinstance(boot["version"], int)
    assert boot["features"]["undo"] is False
    assert {"id", "startsAt", "endsAt"} <= boot["season"].keys()
    snap = boot["snapshot"]
    assert snap["factory"]["credits"] == START_CREDITS
    assert layout_of(snap)["buildings"] == [] and layout_of(snap)["conveyors"] == []
    assert (layout_of(snap)["hangar"]["w"], layout_of(snap)["hangar"]["h"]) == (24, 14)
    tut = order_of(snap, T.TUTORIAL_ORDER_ID)
    assert tut and tut["tutorial"] is True and tut["status"] == "available"

    # intro + the whole tutorial line, command by command; each effective command is exactly version + 1
    for label, cmd in T.INTRO + T.BUILD:
        before = api.version
        body = api.command(cmd)
        assert body["version"] == before + 1, label
        assert body["replayed"] is False, label
        snap = body["snapshot"]
    assert snap["player"]["ageConfirmed"] is True
    assert snap["factory"]["name"] == "E2E Kvas"
    assert 0 < snap["factory"]["credits"] < START_CREDITS

    # the layout on the server == tutorialLayout()
    layout = layout_of(snap)
    got = {b["kind"]: (b["x"], b["y"], b["rot"]) for b in layout["buildings"]}
    assert got == T.EXPECTED_BUILDINGS
    belts = sorted((t["x"], t["y"], t["dir"]) for t in layout["conveyors"] if t["kind"] == "belt")
    assert belts == [(x, T.ROW, "E") for x in T.EXPECTED_BELT_XS]
    assert sorted((t["x"], t["y"], t["dir"]) for t in layout["conveyors"] if t["kind"] == "pipe") == T.EXPECTED_PIPES
    assert {"q_intro", "q_brew", "q_bottles", "q_filler", "q_line"} <= set(snap["dialogs"]["done"])

    # accept the tutorial order on the dock: the server starts loading
    dock = building(snap, "dock")
    credits_before = snap["factory"]["credits"]
    body = api.command({"type": "acceptOrder", "orderId": T.TUTORIAL_ORDER_ID, "dockId": dock["id"]})
    tut = order_of(body["snapshot"], T.TUTORIAL_ORDER_ID)
    assert tut["status"] in ("loading", "done") and tut["dockId"] == dock["id"]
    accepted_version = api.version

    if ORDER_WAIT_S <= 0:  # E2E_ORDER_WAIT_S=0: stop after "loading started" (production needs real time)
        return

    # Loading and closing are the server's job (no client commands). The line ships 2 boxes ~30 s after accepting
    # in real time (measured on the sim: 12 bottles at the starting machine speeds); lazy catch-up on factory/me
    # advances it. Passive polls must never move the version.
    deadline = time.monotonic() + ORDER_WAIT_S
    while True:
        me = api.me()
        assert me["version"] == accepted_version, "passive factory/me must not bump the version"
        tut = order_of(me["snapshot"], T.TUTORIAL_ORDER_ID)
        if tut["status"] == "done":
            break
        assert tut["status"] == "loading"
        assert time.monotonic() < deadline, f"tutorial order not shipped in {ORDER_WAIT_S}s: {tut}"
        time.sleep(4)
    snap = me["snapshot"]
    assert tut["loaded"] == tut["qty"] == 2
    assert KVAS in snap["factory"]["collection"]
    assert snap["factory"]["credits"] > credits_before + 500  # +1000 reward, minus raw material for the running line
    assert snap["factory"]["stars"] > 0 and snap["factory"]["xp"] > 0

    # last quest step: q_dock is marked after the shipment (outro)
    body = api.command({"type": "dialogDone", "id": "q_dock"})
    assert body["version"] == accepted_version + 1
    assert "q_dock" in body["snapshot"]["dialogs"]["done"]


# --------------------------------------------------------------------------- (b)
def test_b_reload_does_not_lose_factory(api: Api):
    api.bootstrap()
    api.command({"type": "dialogDone", "id": "q_intro"})
    api.command({"type": "renameFactory", "name": "Reload Test"})
    body = api.command(T.BUILD[0][1])  # brew_tank
    snap, version = body["snapshot"], body["version"]
    assert snap["factory"]["credits"] == START_CREDITS - 350

    reloaded = api.clone()  # new page load: new connection, no local state, same Telegram user
    again = reloaded.bootstrap()
    assert again["version"] == version, "bootstrap must not bump the version"
    s2 = again["snapshot"]
    assert layout_of(s2)["buildings"] == layout_of(snap)["buildings"]
    assert layout_of(s2)["conveyors"] == layout_of(snap)["conveyors"]
    assert s2["factory"]["credits"] == snap["factory"]["credits"]
    assert s2["factory"]["name"] == "Reload Test"
    assert s2["player"]["id"] == snap["player"]["id"]
    assert set(s2["dialogs"]["done"]) == {"q_intro"}
    me = reloaded.me()
    assert me["version"] == version and me["degraded"] is False
    assert layout_of(me["snapshot"])["buildings"] == layout_of(snap)["buildings"]


# --------------------------------------------------------------------------- (c)
def test_c_two_tabs_passive_sync_never_409(api: Api):
    actor = api
    actor.bootstrap()
    actor.command({"type": "dialogDone", "id": "q_intro"})
    tabs = [actor.clone(), actor.clone()]
    for t in tabs:
        t.bootstrap()

    stop = threading.Event()
    seen: list[list[int]] = [[], []]
    statuses: list[int] = []

    def poll(i: int) -> None:
        # 2 tabs x one poll / 2 s = well inside 60 factory/me per minute per user
        while not stop.is_set():
            r = tabs[i].http.get("/api/factory/me")
            statuses.append(r.status_code)
            if r.status_code == 200:
                seen[i].append(r.json()["version"])
            stop.wait(2.0)

    threads = [threading.Thread(target=poll, args=(i,), daemon=True) for i in range(2)]
    for th in threads:
        th.start()
    try:
        start_version = actor.version
        for n in range(8):  # the "active" tab keeps playing on its own (never refreshed) version
            body = actor.command({"type": "renameFactory", "name": f"Tabs {n}"})
            assert body["version"] == start_version + n + 1
            time.sleep(1.2)
    finally:
        stop.set()
        for th in threads:
            th.join(timeout=10)

    assert statuses and all(s == 200 for s in statuses), statuses  # polls: no 409, no 429
    assert actor.throttled == 0 and all(t.throttled == 0 for t in tabs)
    for versions in seen:
        assert versions == sorted(versions) and versions[-1] <= actor.version
    assert tabs[0].me()["version"] == actor.version  # polls never changed the version


# --------------------------------------------------------------------------- (d)
def test_d_stale_version_409_resync_retry(api: Api):
    tab_a, tab_b = api, api.clone()
    tab_a.bootstrap()
    tab_b.bootstrap()
    stale = tab_a.version
    assert tab_b.version == stale

    tab_b.command({"type": "renameFactory", "name": "From B"})  # B moves the factory on
    key = uuid.uuid4().hex
    cmd = {"type": "renameFactory", "name": "From A"}

    r = tab_a.raw_command(cmd, version=stale, key=key)  # A acts on the stale version
    assert r.status_code == 409, r.text
    e = err(r)
    assert e["code"] == "version_conflict" and e["details"]["current_version"] == stale + 1

    me = tab_a.me()  # resync: factory/me, itself never 409
    assert me["version"] == stale + 1
    assert me["snapshot"]["factory"]["name"] == "From B"  # the 409 changed nothing

    r = tab_a.raw_command(cmd, version=me["version"], key=key)  # retry: same key, new version
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["replayed"] is False and body["version"] == stale + 2
    assert body["snapshot"]["factory"]["name"] == "From A"
    assert tab_b.me()["version"] == stale + 2


# --------------------------------------------------------------------------- (e)
def test_e_replay_same_key_does_not_charge_twice(api: Api):
    boot = api.bootstrap()
    v0 = boot["version"]
    cmd = {"type": "place", "kind": "intake", "x": 0, "y": 5, "rot": 0}  # 150 credits
    key = uuid.uuid4().hex

    r1 = api.raw_command(cmd, version=v0, key=key)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["replayed"] is False and b1["version"] == v0 + 1
    assert b1["snapshot"]["factory"]["credits"] == START_CREDITS - 150

    # network retry of the same request (stale version in the body: replay is checked BEFORE the version)
    for version in (v0, v0 + 1):
        r2 = api.raw_command(cmd, version=version, key=key)
        assert r2.status_code == 200, r2.text
        b2 = r2.json()
        assert b2["ok"] is True and b2["replayed"] is True
        assert b2["version"] == v0 + 1, "a replay does not bump the version"
        assert b2["snapshot"]["factory"]["credits"] == START_CREDITS - 150
        assert len(layout_of(b2["snapshot"])["buildings"]) == 1

    me = api.me()
    assert me["version"] == v0 + 1
    assert me["snapshot"]["factory"]["credits"] == START_CREDITS - 150
    assert len(layout_of(me["snapshot"])["buildings"]) == 1

    # a rejected command is recorded too: its replay is the same rejection, nothing is charged
    key2 = uuid.uuid4().hex
    bad1 = api.raw_command(cmd, version=v0 + 1, key=key2)  # the cell is taken now
    bad2 = api.raw_command(cmd, version=v0 + 1, key=key2)
    assert bad1.status_code == bad2.status_code == 422
    assert err(bad1)["code"] == err(bad2)["code"]
    assert err(bad1)["details"]["message"] == err(bad2)["details"]["message"]
    assert api.me()["snapshot"]["factory"]["credits"] == START_CREDITS - 150


# --------------------------------------------------------------------------- (f)
def test_f_sixth_shift_of_the_day_rejected(api: Api):
    api.bootstrap()
    body = api.command(T.BUILD[0][1])  # brew_tank: a station whose profession is 'kvasovar'
    station = building(body["snapshot"], "brew_tank")["id"]
    assert body["snapshot"]["season"]["shiftsLeft"] == SHIFTS_PER_DAY

    for n in range(SHIFTS_PER_DAY):
        before = api.version
        body = api.command({"type": "submitShift", "buildingId": station, "result": shift_result()})
        assert body["version"] == before + 1
        assert body["snapshot"]["season"]["shiftsLeft"] == SHIFTS_PER_DAY - n - 1

    v = api.version
    r = api.raw_command({"type": "submitShift", "buildingId": station, "result": shift_result()})
    assert r.status_code == 422, r.text
    e = err(r)
    assert e["code"] == "daily_limit_exceeded"
    assert e["details"]["message"]
    assert e["details"]["version"] == v  # rejected: no version bump
    assert e["details"]["snapshot"]["season"]["shiftsLeft"] == 0

    # the limit is server truth (part of factories.state): a reload / another tab does not reset it
    other = api.clone()
    boot = other.bootstrap()
    assert boot["version"] == v and boot["snapshot"]["season"]["shiftsLeft"] == 0
    r = other.raw_command({"type": "submitShift", "buildingId": station, "result": shift_result()})
    assert r.status_code == 422 and err(r)["code"] == "daily_limit_exceeded"


def test_f2_shift_plausibility_codes(api: Api):
    api.bootstrap()
    body = api.command(T.BUILD[0][1])
    station = building(body["snapshot"], "brew_tank")["id"]
    v = api.version
    short = api.raw_command({"type": "submitShift", "buildingId": station, "result": shift_result(durationMs=100)})
    assert short.status_code == 422 and err(short)["code"] == "minigame_too_short"
    impossible = api.raw_command({"type": "submitShift", "buildingId": station, "result": shift_result(score=1.5)})
    assert impossible.status_code == 422 and err(impossible)["code"] == "impossible_score"
    me = api.me()
    assert me["version"] == v
    assert me["snapshot"]["season"]["shiftsLeft"] == SHIFTS_PER_DAY  # rejected shifts are not counted


# --------------------------------------------------------------------------- (g)
def test_g_build_end_does_not_change_version(api: Api):
    boot = api.bootstrap()
    api.command(T.BUILD[0][1])
    v = api.version
    assert v == boot["version"] + 1
    for _ in range(3):
        r = api.raw_command({"type": "buildEnd"}, version=v)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["version"] == v and body["replayed"] is False
        assert body["snapshot"] is None
    assert api.me()["version"] == v
    # buildEnd did not consume the version: the next real command on the same version succeeds (no 409)
    r = api.raw_command({"type": "renameFactory", "name": "After End"}, version=v)
    assert r.status_code == 200 and r.json()["version"] == v + 1


# --------------------------------------------------------------------------- (h)
def test_h_undo_redo_tick_rejected(api: Api):
    boot = api.bootstrap()
    v = boot["version"]
    assert boot["features"]["undo"] is False
    for cmd, reason in (
        ({"type": "undo"}, "history_unavailable"),
        ({"type": "redo"}, "history_unavailable"),
        ({"type": "tick", "from": 1_700_000_000_000, "to": 1_700_000_005_000, "events": []}, "tick_removed"),
    ):
        r = api.raw_command(cmd, version=v)
        assert r.status_code == 422, f"{cmd['type']}: {r.status_code} {r.text}"
        e = err(r)
        assert e["code"] == "command_rejected", e
        assert e["details"]["reason"] == reason
        assert e["details"]["message"] and e["details"]["version"] == v
        assert e["details"]["snapshot"]["factory"]["credits"] == START_CREDITS
    assert api.me()["version"] == v
