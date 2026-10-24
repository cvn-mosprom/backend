"""S7 smoke suite (spec block "S7": 20-50 scenarios, two tabs, ragged network) against the running compose stack.

    cd e2e && .venv/Scripts/python -m pytest test_s7_smoke.py -q          # ~3 min, stack must be up
    E2E_DOCKER=1 ... -m pytest test_s7_smoke.py -q                        # + the sim-outage scenario (stops the sim container)

Every scenario uses fresh random dev users. EVERY response of every `SmokeApi` tab is validated on the fly (smoke/checks.py):
X-Request-ID present, JSON content-type, error bodies are exactly {"error": {"code","message","details"}}, no stack traces /
framework internals, no 500, a 429 always has Retry-After. A violation fails the test at teardown with the request that caused it.

Known product bugs found by this suite are kept as `xfail(strict=False)` scenarios (BUGS below) so the suite stays green and
flips to XPASS once they are fixed. Resilience of restarts / dead sim is covered by test_s7_resilience.py and test_s5_*.
"""

import json
import os
import random
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx
import pytest
import tutorial as T
from conftest import BASE, Api, building, layout_of, order_of
from smoke import rawhttp
from smoke.checks import problems
from smoke.client import BELT_COST, SmokeApi, belt, par
from smoke.routes import ROUTE_IDS, ROUTES
from smoke.stack import DOCKER, compose, wait_ready, wait_sim_healthy

ROOT = Path(__file__).resolve().parents[1]
START_CREDITS = 3000
KVAS = "kvas_ochakovsky_15"
ORDER_WAIT_S = float(os.environ.get("E2E_ORDER_WAIT_S", "120"))
MISSING = object()

try:  # the correct quiz options are NOT public: read them from the bank source (pure dataclasses), like the S5 e2e
    sys.path.insert(0, str(ROOT / "api"))
    from app import quiz_bank

    CORRECT = {q.id: q.correct_id for q in quiz_bank.QUESTIONS}  # S9: option ids a|b|c
except Exception:  # pragma: no cover - then the quiz is answered with 0 and only checked for consistency
    CORRECT = {}

xfail_bug = lambda why: pytest.mark.xfail(strict=False, reason=f"BUG: {why}")  # noqa: E731


# ------------------------------------------------------------------------------------------ fixtures / helpers
@pytest.fixture
def sink():
    """Violations collected by all tabs of the test; asserted empty at teardown."""
    found: list[str] = []
    yield found
    assert not found, "response invariants violated:\n  " + "\n  ".join(found[:15])


@pytest.fixture
def new_user(sink):
    made: list[SmokeApi] = []

    def make(allow_503: bool = False) -> SmokeApi:
        a = SmokeApi(random.randint(10**9, 10**10 - 1), sink=sink, allow_503=allow_503)
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


@pytest.fixture
def api(new_user) -> SmokeApi:
    return new_user()


def credits_of(snapshot) -> int:
    return snapshot["factory"]["credits"]


def plan(snapshot):
    """The layout without volatile fields (`wear` creeps up with every lazy catch-up once a line runs)."""
    lay = layout_of(snapshot)
    return (sorted((b["id"], b["kind"], b["x"], b["y"], b["rot"]) for b in lay["buildings"]),
            sorted((c["kind"], c["x"], c["y"], c["dir"]) for c in lay["conveyors"]))


def tiles(snapshot) -> int:
    return len(layout_of(snapshot)["conveyors"])


def years_ago(years: int) -> str:
    t = time.gmtime()
    return f"{t.tm_year - years}-{t.tm_mon:02d}-{min(t.tm_mday, 28):02d}"


def lead_body(**over):
    body = {"fullName": "Смоук Тестов", "birthDate": years_ago(22), "phone": "+7 999 111-22-33", "telegram": "@smoke_tester",
            "university": "РХТУ им. Менделеева", "direction": "Пищевая технология", "course": "4", "vacancy": "lab",
            "readiness": "season", "practiceType": "paid", "consent": True, "consentTextVersion": "2026-10-v1"}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def shift_cmd(station: str) -> dict:
    return {"type": "submitShift", "buildingId": station, "result": {
        "gameId": "smoke_stub", "profession": "kvasovar", "result": "win", "score": 0.8, "durationMs": 6000, "difficulty": 0.5,
        "metrics": {"accuracy": 0.8, "misses": 0}}}


def with_station(api: SmokeApi) -> str:
    """Bootstrap + a brew tank (the kvasovar station, 350 credits); returns its building id."""
    api.bootstrap()
    return building(api.command(T.BUILD[0][1])["snapshot"], "brew_tank")["id"]


def code_of(r: httpx.Response) -> str:
    return r.json()["error"]["code"]


def ev(name: str = "app_open", **props) -> dict:
    return {"name": name, "ts": int(time.time() * 1000), **({"props": props} if props else {})}


# ========================================================================================== A. demo path
def test_demo_path_end_to_end(api: SmokeApi):
    """bootstrap -> tutorial line -> accept order -> done -> leaderboard -> summary -> quiz -> lead -> leads/me -> revoke."""
    boot = api.bootstrap()
    pid = boot["snapshot"]["player"]["id"]
    for label, cmd in T.INTRO + T.BUILD:
        before = api.version
        body = api.command(cmd)
        assert body["version"] == before + 1 and body["replayed"] is False, label
    snap = body["snapshot"]
    assert {b["kind"]: (b["x"], b["y"], b["rot"]) for b in layout_of(snap)["buildings"]} == T.EXPECTED_BUILDINGS

    body = api.command({"type": "acceptOrder", "orderId": T.TUTORIAL_ORDER_ID, "dockId": building(snap, "dock")["id"]})
    accepted_version = api.version
    shipped = ORDER_WAIT_S > 0
    if shipped:  # ~30 s of real time; passive polls never move the version
        deadline = time.monotonic() + ORDER_WAIT_S
        while True:
            me = api.me()
            assert me["version"] == accepted_version
            tut = order_of(me["snapshot"], T.TUTORIAL_ORDER_ID)
            if tut["status"] == "done":
                break
            assert time.monotonic() < deadline, f"order not shipped in {ORDER_WAIT_S}s: {tut}"
            time.sleep(3)
        assert KVAS in me["snapshot"]["factory"]["collection"]
        api.command({"type": "dialogDone", "id": "q_dock"})
    me = api.me()
    snap = me["snapshot"]
    stars, level = snap["factory"]["stars"], snap["factory"]["level"]
    assert (stars > 0) is shipped

    # leaderboard: every tab has a `me` block that is this player; general value == stars; place agrees with snapshot.rank
    lbs = {}
    for tab in ("general", "quality", "speed", "uni"):
        r = api.http.get("/api/season/leaderboard", params={"tab": tab})
        assert r.status_code == 200, r.text
        lbs[tab] = r.json()
        assert lbs[tab]["me"]["isMe"] is True and lbs[tab]["me"]["playerId"] == pid, tab
        assert 1 <= lbs[tab]["me"]["place"] <= lbs[tab]["total"]
    general = lbs["general"]["me"]
    assert general["value"] == stars
    assert abs(general["place"] - snap["rank"]["place"]) <= 5  # bots may grow between the two reads
    if shipped:
        assert lbs["quality"]["me"]["value"] > 0
        # S13: the e2e player is a test user (users.is_test): ranked in `me`, but never listed in the shared top
        rows = api.http.get("/api/season/leaderboard", params={"tab": "general", "limit": 100}).json()["rows"]
        assert not [x for x in rows if x["playerId"] == pid], "a test player must not be listed in the top"

    # season summary
    s = api.http.get("/api/season/summary").json()
    assert s["stars"] == stars and s["level"] == level and s["credits"] > 0
    assert abs(s["place"] - general["place"]) <= 5 and s["quizResult"] is None and s["leadStatus"] is None
    if shipped:
        assert s["bottles"] > 0 and s["lines"] >= 1

    # quiz in two batches
    q = api.http.get("/api/quiz/questions").json()
    ids = [x["id"] for x in q["questions"]]
    assert len(ids) == 8 and q["progress"]["answered"] == 0 and "correct" not in json.dumps(q["questions"])
    answers = [{"question_id": i, "answer": CORRECT.get(i, 0)} for i in ids]
    r1 = api.http.post("/api/quiz/answer", json={"answers": answers[:4]})
    assert r1.status_code == 200 and r1.json()["answered"] == 4 and r1.json()["completed"] is False
    r2 = api.http.post("/api/quiz/answer", json=answers[4:])
    assert r2.status_code == 200 and r2.json()["completed"] is True
    correct = r2.json()["correct"]
    assert correct == sum(x["correct"] for x in r1.json()["results"] + r2.json()["results"])
    assert r2.json()["total"] == 8 and r2.json()["score"] == round(100 * correct / 8)
    if CORRECT:
        assert correct == 8 and r2.json()["score"] == 100
    assert api.http.get("/api/season/summary").json()["quizResult"] == {
        "correct": correct, "total": 8, "score": r2.json()["score"], "answered": 8, "completed": True, "bankTotal": 8}
    score = correct

    # lead -> leads/me -> summary status -> events -> revoke (idempotent)
    r = api.http.post("/api/leads", json=lead_body())
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["status"] == "new" and lead["quizScore"] == score and ("high_quiz" in lead["tags"]) is (score >= 6)
    mine = api.http.get("/api/leads/me").json()
    assert mine["id"] == lead["id"] and "hrNote" not in mine and mine["consentRevokedAt"] is None
    assert api.http.get("/api/season/summary").json()["leadStatus"] == "new"
    assert api.http.post("/api/events", json={"events": [ev(), ev("game_end", result="win", duration_ms=4200)]}).json() == {"accepted": 2, "rejected": 0}
    assert api.http.delete("/api/leads/me").status_code == 204
    gone = api.http.get("/api/leads/me")  # the revoked form is forgotten for the owner (the row stays in the DB)
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "lead_not_found"
    assert api.http.get("/api/season/summary").json()["leadStatus"] == "revoked"
    assert api.http.delete("/api/leads/me").status_code == 204
    assert api.me()["version"] == api.version, "the whole non-game tail never touched the factory version"


def test_second_tab_mirrors_every_tutorial_step(api: SmokeApi):
    """A second tab (passive factory/me after each step) sees exactly what the acting tab built, and is never behind."""
    api.bootstrap()
    mirror = api.clone()
    for label, cmd in T.INTRO + T.BUILD:
        body = api.command(cmd)
        me = mirror.me()
        assert me["version"] == body["version"] and me["degraded"] is False, label
        assert plan(me["snapshot"]) == plan(body["snapshot"]), label
        assert abs(credits_of(me["snapshot"]) - credits_of(body["snapshot"])) <= 5, label  # a finished line starts consuming raw material
    mirror.version = me["version"]
    assert mirror.play({"type": "renameFactory", "name": "Mirror Tab"}).conflicts == 0  # up to date through passive sync alone
    stale = api.play({"type": "renameFactory", "name": "Acting Tab"})
    assert stale.ok and stale.conflicts == 1 and stale.version == mirror.version + 1


# ========================================================================================== B. two tabs
def test_two_tabs_interleaved_commands_converge(api: SmokeApi):
    """Strictly alternating tabs, each on its own cached version: every turn but the first is 409 -> resync -> retry."""
    a, b = api, api.clone()
    a.bootstrap()
    b.bootstrap()
    versions = []
    for i in range(12):
        tab, row = (a, 2) if i % 2 == 0 else (b, 8)
        played = tab.play(belt(i // 2, row))
        assert played.ok and played.body["replayed"] is False, i
        assert played.conflicts == (0 if i == 0 else 1), i
        versions.append(played.version)
    assert versions == list(range(1, 13)) and a.conflicts + b.conflicts == 11
    fa, fb = a.me(), b.me()
    assert fa["version"] == fb["version"] == 12
    assert plan(fa["snapshot"]) == plan(fb["snapshot"]) and tiles(fa["snapshot"]) == 12
    assert credits_of(fa["snapshot"]) == START_CREDITS - 12 * BELT_COST  # nothing charged twice, nothing lost


@pytest.mark.parametrize("n_tabs,per_tab", [(2, 8), (3, 5)])
def test_tabs_racing_concurrently_lose_no_update(api: SmokeApi, n_tabs: int, per_tab: int):
    """Tabs fire at the same instant on thread barriers; 409 -> factory/me -> retry converges to ONE serial history."""
    tabs = [api] + [api.clone() for _ in range(n_tabs - 1)]
    for t in tabs:
        t.bootstrap()
    rows = [2, 4, 6][:n_tabs]  # every tab owns a row: lost updates would leave holes in it

    def run(t: SmokeApi, row: int):
        return [t.play(belt(x, row)) for x in range(per_tab)]

    results = par([lambda t=t, row=row: run(t, row) for t, row in zip(tabs, rows)])
    played = [p for rs in results for p in rs]
    total = n_tabs * per_tab
    assert all(p.ok and p.body["replayed"] is False for p in played)
    assert sorted(p.version for p in played) == list(range(1, total + 1)), "every command got its own version (one serial history)"
    me = api.me()
    assert me["version"] == total and tiles(me["snapshot"]) == total
    assert credits_of(me["snapshot"]) == START_CREDITS - total * BELT_COST
    for t, row in zip(tabs, rows):  # nothing of any tab was overwritten
        assert sorted(c["x"] for c in layout_of(me["snapshot"])["conveyors"] if c["y"] == row) == list(range(per_tab))


def test_passive_polling_never_conflicts(api: SmokeApi):
    """bootstrap / advance / factory/me of tab B between the commands of tab A: A's cached version stays valid (never 409)."""
    a, b = api, api.clone()
    a.bootstrap()
    passive = [lambda: b.bootstrap(), lambda: b.advance(cap_hours=1, reason="resume").json(), lambda: b.me()]
    for i in range(9):
        r = a.send(belt(i, 2))
        assert r.status_code == 200, f"turn {i}: {r.status_code} {r.text[:200]}"
        a.version = r.json()["version"]
        got = passive[i % 3]()
        assert got["version"] == a.version, f"passive call #{i % 3} must not change the version"
    b.version = a.version
    assert b.play(belt(0, 8)).conflicts == 0  # B, synced only passively, acts on a valid version
    assert a.play(belt(1, 8)).conflicts == 1  # and now A is the stale one
    assert a.throttled == b.throttled == 0


@pytest.mark.parametrize("stale", [0, 2, 3, 10**9, 2**63, 2**64, 10**100], ids=lambda v: f"v{v:.3g}")
def test_wrong_version_is_409_and_does_not_burn_the_key(api: SmokeApi, stale: int):
    api.bootstrap()
    api.command(belt(0, 2))  # server version 1
    key = uuid.uuid4().hex
    r = api.send(belt(1, 2), version=stale, key=key)
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict", r.text
    assert r.json()["error"]["details"] == {"current_version": 1, "version": 1}
    me = api.me()
    assert me["version"] == 1 and tiles(me["snapshot"]) == 1 and credits_of(me["snapshot"]) == START_CREDITS - BELT_COST
    ok = api.send(belt(1, 2), version=1, key=key)  # the 409 recorded nothing: the same key now applies, once
    assert ok.status_code == 200 and ok.json()["replayed"] is False and ok.json()["version"] == 2


def test_first_launch_in_many_tabs_creates_one_factory(api: SmokeApi):
    """The Mini App opened in 8 tabs at once by a brand-new player: one user, one factory, same answer everywhere."""
    tabs = [api] + [api.clone() for _ in range(7)]
    res = par([lambda t=t: t.http.get("/api/bootstrap") for t in tabs])
    assert [r.status_code for r in res] == [200] * 8, [r.text[:120] for r in res if r.status_code != 200]
    assert len({r.json()["snapshot"]["player"]["id"] for r in res}) == 1
    assert {r.json()["version"] for r in res} == {0}
    assert {credits_of(r.json()["snapshot"]) for r in res} == {START_CREDITS}
    api.version = 0
    assert api.play(belt(0, 2)).version == 1


def test_two_players_in_parallel_do_not_interfere(new_user):
    x, y = new_user(), new_user()
    x.bootstrap()
    y.bootstrap()
    rx, ry = par([lambda: [x.play(belt(i, 2)) for i in range(8)], lambda: [y.play(belt(i, 9)) for i in range(8)]])
    assert [p.version for p in rx] == [p.version for p in ry] == list(range(1, 9))
    assert x.conflicts == y.conflicts == 0
    mx, my = x.me(), y.me()
    assert {c["y"] for c in layout_of(mx["snapshot"])["conveyors"]} == {2} and {c["y"] for c in layout_of(my["snapshot"])["conveyors"]} == {9}
    assert credits_of(mx["snapshot"]) == credits_of(my["snapshot"]) == START_CREDITS - 8 * BELT_COST


def test_concurrent_shift_storm_respects_the_daily_limit(api: SmokeApi):
    """4 tabs x 3 shift attempts, all racing: exactly 5 are accepted (shifts per day), the rest daily_limit_exceeded."""
    station = with_station(api)
    v0 = api.version
    tabs = [api] + [api.clone() for _ in range(3)]
    for t in tabs[1:]:
        t.bootstrap()
    results = par([lambda t=t: [t.play(shift_cmd(station)) for _ in range(3)] for t in tabs])
    played = [p for rs in results for p in rs]
    ok = [p for p in played if p.ok]
    rejected = [p for p in played if not p.ok]
    assert len(ok) == 5 and sorted(p.version for p in ok) == list(range(v0 + 1, v0 + 6))
    assert len(rejected) == 7 and {p.body["error"]["code"] for p in rejected} == {"daily_limit_exceeded"}
    me = api.me()
    assert me["version"] == v0 + 5 and me["snapshot"]["season"]["shiftsLeft"] == 0


# ========================================================================================== C. flaky network
def test_duplicate_requests_in_flight_apply_once(api: SmokeApi):
    """A client (or proxy) that resends the same request 6x at once: one application, six consistent answers."""
    api.bootstrap()
    tabs = [api.clone() for _ in range(6)]
    key, cmd = uuid.uuid4().hex, {"type": "place", "kind": "intake", "x": 0, "y": 5, "rot": 0}  # 150 credits
    res = par([lambda t=t: t.send(cmd, version=0, key=key) for t in tabs])
    assert [r.status_code for r in res] == [200] * 6, [r.text[:100] for r in res]
    assert Counter(r.json()["replayed"] for r in res) == {False: 1, True: 5}
    assert {r.json()["version"] for r in res} == {1}
    me = api.me()
    assert me["version"] == 1 and credits_of(me["snapshot"]) == START_CREDITS - 150 and len(layout_of(me["snapshot"])["buildings"]) == 1


def test_lost_response_resend_after_another_tab_moved_on(api: SmokeApi):
    """Tab A's response got lost; tab B changed the factory meanwhile; A resends with its OLD version -> replay, not 409, not twice."""
    a, b = api, api.clone()
    a.bootstrap()
    b.bootstrap()
    key = uuid.uuid4().hex
    first = a.send(belt(0, 2), version=0, key=key)
    assert first.status_code == 200 and first.json()["version"] == 1  # ... which "never arrived"
    b.version = 1
    assert b.play(belt(0, 8)).version == 2
    for _ in range(3):
        again = a.send(belt(0, 2), version=0, key=key)
        assert again.status_code == 200 and again.json()["replayed"] is True and again.json()["version"] == 2
    me = a.me()
    assert tiles(me["snapshot"]) == 2 and credits_of(me["snapshot"]) == START_CREDITS - 2 * BELT_COST


def test_idempotency_key_scope(new_user):
    """A key is per player and per command: reuse across players is independent; reuse for another command replays the first."""
    a, b = new_user(), new_user()
    a.bootstrap()
    b.bootstrap()
    key = uuid.uuid4().hex
    assert a.send({"type": "renameFactory", "name": "First"}, version=0, key=key).status_code == 200
    second = a.send({"type": "renameFactory", "name": "Second"}, version=1, key=key)
    assert second.status_code == 200 and second.json()["replayed"] is True
    assert a.me()["snapshot"]["factory"]["name"] == "First", "the second command with a used key must not run"
    other = b.send({"type": "renameFactory", "name": "Other Player"}, version=0, key=key)
    assert other.status_code == 200 and other.json()["replayed"] is False
    assert b.me()["snapshot"]["factory"]["name"] == "Other Player"


def test_rejected_command_resent_concurrently_gets_the_same_rejection(api: SmokeApi):
    api.bootstrap()
    api.command(T.BUILD[0][1])  # brew_tank at (6,1)
    v, key = api.version, uuid.uuid4().hex
    tabs = [api.clone() for _ in range(5)]
    res = par([lambda t=t: t.send(T.BUILD[0][1], version=v, key=key) for t in tabs])  # the cell is taken
    assert {r.status_code for r in res} == {422} and {code_of(r) for r in res} == {"command_rejected"}
    assert len({r.json()["error"]["details"]["message"] for r in res}) == 1
    me = api.me()
    assert me["version"] == v and credits_of(me["snapshot"]) == START_CREDITS - 350


def test_duplicate_submit_shift_gives_one_reward(api: SmokeApi):
    station = with_station(api)
    v0 = api.version
    pre = api.me()["snapshot"]
    assert pre["season"]["shiftsLeft"] == 5 and pre["factory"]["xp"] == 0
    tabs = [api.clone() for _ in range(6)]
    key = uuid.uuid4().hex
    res = par([lambda t=t: t.send(shift_cmd(station), version=v0, key=key) for t in tabs])
    assert [r.status_code for r in res] == [200] * 6, [r.text[:100] for r in res]
    fresh = [r for r in res if r.json()["replayed"] is False]
    assert len(fresh) == 1 and fresh[0].json()["result"]["rewards"] == {"xp": 10}
    assert {r.json()["version"] for r in res} == {v0 + 1}
    post = api.me()["snapshot"]
    assert post["factory"]["xp"] == 10 and post["season"]["shiftsLeft"] == 4 and api.me()["version"] == v0 + 1


def test_duplicate_lead_and_revoke_from_three_tabs(api: SmokeApi):
    tabs = [api] + [api.clone() for _ in range(2)]
    res = par([lambda t=t: t.http.post("/api/leads", json=lead_body()) for t in tabs])
    assert sorted(r.status_code for r in res) == [201, 409, 409]
    assert {code_of(r) for r in res if r.status_code == 409} == {"lead_already_exists"}
    lead = next(r.json() for r in res if r.status_code == 201)
    assert api.http.get("/api/leads/me").json()["id"] == lead["id"]
    res = par([lambda t=t: t.http.delete("/api/leads/me") for t in tabs])
    assert [r.status_code for r in res] == [204] * 3
    assert api.http.get("/api/leads/me").status_code == 404
    again = api.http.post("/api/leads", json=lead_body())
    assert again.status_code == 201 and again.json()["id"] != lead["id"]  # a revoked form does not block a new one
    assert api.http.get("/api/leads/me").json()["id"] == again.json()["id"]


def test_duplicate_quiz_submit_from_three_tabs(api: SmokeApi):
    tabs = [api] + [api.clone() for _ in range(2)]
    ids = [q["id"] for q in api.http.get("/api/quiz/questions").json()["questions"]]
    answers = [{"question_id": i, "answer": CORRECT.get(i, 0)} for i in ids]
    res = par([lambda t=t: t.http.post("/api/quiz/answer", json=answers) for t in tabs])
    assert sorted(r.status_code for r in res) == [200, 409, 409]
    assert {code_of(r) for r in res if r.status_code == 409} == {"quiz_already_completed"}
    progress = api.http.get("/api/quiz/questions").json()["progress"]
    assert progress["answered"] == 8 and progress["completed"] is True
    assert progress["score"] == next(r.json() for r in res if r.status_code == 200)["score"] == 100


def test_clients_that_hang_up_before_the_response_then_resend(api: SmokeApi):
    """Raw sockets: the whole request is sent, the connection dies before the answer. The app resends the same key -> applied once."""
    api.bootstrap()
    for i in range(4):
        key = uuid.uuid4().hex
        body = json.dumps({"version": api.version, "idempotency_key": key, "cmd": belt(i, 2)}).encode()
        rawhttp.fire_and_abort(rawhttp.request_bytes("POST", "/api/command", api.tg_id, body, name=api.name))
        r = api.send(belt(i, 2), version=api.version, key=key)  # the retry: the original may or may not have run already
        assert r.status_code == 200, r.text[:200]
        assert r.json()["version"] == i + 1
        api.version = i + 1
    time.sleep(1.0)  # an abandoned original that was still queued must not apply a second time
    me = api.me()
    assert me["version"] == 4 and tiles(me["snapshot"]) == 4 and credits_of(me["snapshot"]) == START_CREDITS - 4 * BELT_COST


def test_truncated_and_garbage_requests_leave_the_api_healthy(api: SmokeApi):
    api.bootstrap()
    body = b'{"version": 0, "idempotency_key": "trunc'
    status, _ = rawhttp.exchange(rawhttp.request_bytes("POST", "/api/command", api.tg_id, body, content_length=900, name=api.name), 2.0)
    assert status in (None, 400, 408, 422, 499, 502, 503), status  # a half-sent body: any clean "no", never a 500
    for junk in (b"\x00\x01\x02garbage\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"POST /api/command HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\n\r\n"):
        status, _ = rawhttp.exchange(junk, 2.0)
        assert status in (None, 400, 404, 505), (junk, status)
    assert httpx.get(f"{BASE}/ready", verify=False, timeout=10).status_code == 200
    me = api.me()
    assert me["version"] == 0 and api.play(belt(0, 2)).version == 1


def test_rate_limit_bursts_429_retry_after_honoured(new_user):
    """Burst each bucket until it throttles (429 + Retry-After, exact limit), other players are unaffected, waiting
    Retry-After makes it work again, and a throttled command was NOT applied (its key is still usable). Buckets in parallel (~60 s)."""
    fire = {
        "advance": lambda a: a.http.post("/api/advance", json={"cap_hours": 0}),
        "bootstrap": lambda a: a.http.get("/api/bootstrap"),
        "events": lambda a: a.http.post("/api/events", json={"events": [ev()]}),
        "leads": lambda a: a.http.post("/api/leads", json={}),  # 422 every time, but every attempt counts
        "command": lambda a: a.http.post("/api/command", json={"version": 0, "idempotency_key": uuid.uuid4().hex, "cmd": {"type": "buildEnd"}}),
    }

    def burst(name: str) -> dict:
        a, bystander = new_user(), new_user()
        a.bootstrap()
        hits = 1 if name == "bootstrap" else 0
        for _ in range(250):
            r = fire[name](a)
            if r.status_code == 429:
                break
            hits += 1
        else:
            raise AssertionError(f"{name}: no 429 after 250 requests")
        err = r.json()["error"]
        retry = int(r.headers["Retry-After"])
        assert err["code"] == "rate_limited" and err["details"]["retry_after"] == retry and 1 <= retry <= err["details"]["window_s"]
        assert hits == err["details"]["limit"], f"{name}: {hits} requests were let through, the limit says {err['details']['limit']}"
        assert fire[name](a).status_code == 429  # still throttled inside the window
        assert bystander.http.get("/api/me").status_code == 200  # the limit is per player
        key = uuid.uuid4().hex
        if name == "command":
            denied = a.http.post("/api/command", json={"version": 0, "idempotency_key": key, "cmd": {"type": "renameFactory", "name": "Throttled"}})
            assert denied.status_code == 429
            me = a.me()  # factory/me has its own bucket
            assert me["version"] == 0 and me["snapshot"]["factory"]["name"] != "Throttled"
        time.sleep(retry + 1.0)
        assert fire[name](a).status_code != 429, f"{name}: still throttled after Retry-After"
        if name == "command":  # the key of the throttled request was never consumed
            ok = a.http.post("/api/command", json={"version": 0, "idempotency_key": key, "cmd": {"type": "renameFactory", "name": "Throttled"}})
            assert ok.status_code == 200 and ok.json()["replayed"] is False and ok.json()["version"] == 1
        return {"hits": hits, "retry_after": retry}

    out = par([lambda n=n: burst(n) for n in fire])
    assert all(1 <= o["retry_after"] <= 60 for o in out)


# ========================================================================================== D. bad input
def cmdbody(**over) -> dict:
    body = {"version": 0, "idempotency_key": "smoke-" + uuid.uuid4().hex[:16], "cmd": {"type": "renameFactory", "name": "Smoke Name"}}
    body.update(over)
    return {k: v for k, v in body.items() if v is not MISSING}


J = {"content-type": "application/json"}
VALID_RAW = json.dumps(cmdbody()).encode()
BAD_COMMAND_BODIES = {  # id -> (request kwargs, status, code)
    "malformed-json": (dict(content=b'{"version":', headers=J), 422, "validation_error"),
    "empty-body": (dict(content=b"", headers=J), 422, "validation_error"),
    "null": (dict(content=b"null", headers=J), 422, "validation_error"),
    "array": (dict(content=b"[]", headers=J), 422, "validation_error"),
    "number": (dict(content=b"42", headers=J), 422, "validation_error"),
    "string": (dict(content=b'"x"', headers=J), 422, "validation_error"),
    "nan-literal": (dict(content=b'{"version": NaN}', headers=J), 422, "validation_error"),
    "invalid-utf8": (dict(content=b"\xff\xfe\x00{", headers=J), 422, "validation_error"),
    "text-plain": (dict(content=VALID_RAW, headers={"content-type": "text/plain"}), 422, "validation_error"),
    "no-content-type": (dict(content=VALID_RAW), 422, "validation_error"),
    "form-encoded": (dict(content=b"version=0&idempotency_key=abcdefgh", headers={"content-type": "application/x-www-form-urlencoded"}), 422, "validation_error"),
    "version-string": (dict(json=cmdbody(version="1")), 422, "validation_error"),
    "version-float": (dict(json=cmdbody(version=1.5)), 422, "validation_error"),
    "version-bool": (dict(json=cmdbody(version=True)), 422, "validation_error"),
    "version-null": (dict(json=cmdbody(version=None)), 422, "validation_error"),
    "version-missing": (dict(json=cmdbody(version=MISSING)), 422, "validation_error"),
    "version-negative": (dict(json=cmdbody(version=-1)), 422, "validation_error"),
    "version-minus-huge": (dict(json=cmdbody(version=-(10**30))), 422, "validation_error"),
    "key-int": (dict(json=cmdbody(idempotency_key=12345678)), 422, "validation_error"),
    "key-null": (dict(json=cmdbody(idempotency_key=None)), 422, "validation_error"),
    "key-missing": (dict(json=cmdbody(idempotency_key=MISSING)), 422, "validation_error"),
    "key-7-chars": (dict(json=cmdbody(idempotency_key="x" * 7)), 422, "validation_error"),
    "key-65-chars": (dict(json=cmdbody(idempotency_key="x" * 65)), 422, "validation_error"),
    "key-100k": (dict(json=cmdbody(idempotency_key="x" * 100_000)), 422, "validation_error"),
    "cmd-string": (dict(json=cmdbody(cmd="renameFactory")), 422, "validation_error"),
    "cmd-null": (dict(json=cmdbody(cmd=None)), 422, "validation_error"),
    "cmd-missing": (dict(json=cmdbody(cmd=MISSING)), 422, "validation_error"),
    "cmd-array": (dict(json=cmdbody(cmd=[{"type": "buildEnd"}])), 422, "validation_error"),
    "cmd-empty-object": (dict(json=cmdbody(cmd={})), 422, "validation_error"),
    "cmd-unknown-type": (dict(json=cmdbody(cmd={"type": "dropDatabase"})), 422, "validation_error"),
    "cmd-type-case": (dict(json=cmdbody(cmd={"type": "BUILDEND"})), 422, "validation_error"),
    "cmd-type-number": (dict(json=cmdbody(cmd={"type": 5})), 422, "validation_error"),
    "cmd-missing-field": (dict(json=cmdbody(cmd={"type": "renameFactory"})), 422, "validation_error"),
    "cmd-wrong-field-type": (dict(json=cmdbody(cmd={"type": "place", "kind": "intake", "x": "a", "y": 1, "rot": 0})), 422, "validation_error"),
    "cmd-bad-enum": (dict(json=cmdbody(cmd={"type": "place", "kind": "nuclear_plant", "x": 0, "y": 0, "rot": 0})), 422, "validation_error"),
    "cmd-bad-rot": (dict(json=cmdbody(cmd={"type": "place", "kind": "intake", "x": 0, "y": 5, "rot": 9})), 422, "validation_error"),
    "cmd-shift-without-result": (dict(json=cmdbody(cmd={"type": "submitShift", "buildingId": "b1"})), 422, "validation_error"),
    "cmd-huge-numbers": (dict(json=cmdbody(cmd={"type": "move", "buildingId": "b1", "x": 1e30, "y": -1e30, "rot": 0})), 422, "command_rejected"),
    "cmd-name-wrong-type": (dict(json=cmdbody(cmd={"type": "renameFactory", "name": ["a"]})), 422, "validation_error"),
    "cmd-pad-1.9MB": (dict(json=cmdbody(cmd={"type": "dropDatabase", "pad": "x" * 1_900_000})), 422, "validation_error"),
    "cmd-unknown-building": (dict(json=cmdbody(cmd={"type": "upgrade", "buildingId": "no-such-building"})), 422, "command_rejected"),
    "cmd-undo": (dict(json=cmdbody(cmd={"type": "undo"})), 422, "command_rejected"),
}


@pytest.mark.parametrize("case", list(BAD_COMMAND_BODIES), ids=list(BAD_COMMAND_BODIES))
def test_command_bad_bodies_are_clean_4xx_and_change_nothing(api: SmokeApi, case: str):
    kwargs, status, code = BAD_COMMAND_BODIES[case]
    boot = api.bootstrap()
    r = api.http.post("/api/command", **kwargs)
    assert r.status_code == status and code_of(r) == code, f"{r.status_code} {r.text[:200]}"
    assert credits_of(r.json()["error"]["details"]["snapshot"]) == START_CREDITS if code == "command_rejected" else True
    me = api.me()
    assert me["version"] == boot["version"] == 0 and credits_of(me["snapshot"]) == START_CREDITS


def test_giant_factory_name_is_truncated_not_stored(api: SmokeApi):
    """A 1.5 MB name (below Caddy's 2 MB cap) is accepted but the sim keeps 24 chars: the state stays small."""
    api.bootstrap()
    p = api.play({"type": "renameFactory", "name": "Z" * 1_500_000})
    assert p.ok and len(p.body["snapshot"]["factory"]["name"]) <= 24
    assert len(api.http.get("/api/factory/me").content) < 5000


def test_command_accepts_odd_but_valid_input(api: SmokeApi):
    """Unicode / emoji / markup in strings are plain data (stored and returned verbatim), not an error and not interpreted."""
    api.bootstrap()
    for i, name in enumerate(["Квас 🍺 «Очаков»", "<b>bold</b><i>x</i>", "'; DROP TABLE users;--", "  padded  ", "名前"]):
        p = api.play({"type": "renameFactory", "name": name}, key="ключ-ключ-😀-" + uuid.uuid4().hex[:8])  # non-ASCII key is fine
        assert p.ok, (name, p.response.text[:200])
        assert api.me()["snapshot"]["factory"]["name"].strip() == name.strip()
    assert api.me()["version"] == 5


def test_oversize_bodies(api: SmokeApi):
    api.bootstrap()
    # API limit on /api/events (256 KB): clean 413 in the unified format
    r = api.http.post("/api/events", content=b"x" * (300 * 1024), headers=J)
    assert r.status_code == 413 and code_of(r) == "payload_too_large" and r.json()["error"]["details"]["max_bytes"] == 262144
    # Caddy's own limit (2 MB, request_body max_size) answers before the API: 413, empty body, no request id (documented)
    with api.unchecked():
        big = api.http.post("/api/command", content=b"x" * (3 * 1024 * 1024), headers=J)
    assert big.status_code == 413
    assert api.me()["version"] == 0  # the connection pool and the API survived both


@pytest.mark.parametrize("method,path,status,code", [
    ("PUT", "/api/command", 405, "method_not_allowed"),
    ("GET", "/api/command", 405, "method_not_allowed"),
    ("POST", "/api/bootstrap", 405, "method_not_allowed"),
    ("DELETE", "/api/bootstrap", 405, "method_not_allowed"),
    ("PATCH", "/api/leads/me", 405, "method_not_allowed"),
    ("OPTIONS", "/api/command", 405, "method_not_allowed"),
    ("GET", "/api/nope", 404, "not_found"),
    ("GET", "/api/factory/me/extra", 404, "not_found"),
    ("GET", "/api/factory/-1", 404, "player_not_found"),
    ("GET", "/api/factory/abc", 404, "player_not_found"),
    ("GET", "/api/factory/%00", 404, "player_not_found"),
    ("GET", "/api/factory/99999999999999999999999", 404, "player_not_found"),
    ("GET", "/api/factory/%E2%82%AC", 404, "player_not_found"),
    ("GET", "/api/season/leaderboard?limit=abc", 422, "validation_error"),
    ("GET", "/api/season/leaderboard?limit=-5", 422, "validation_error"),
    ("GET", "/api/season/leaderboard?limit=1000000", 422, "validation_error"),
    ("GET", "/api/season/leaderboard?tab=%00", 422, "invalid_tab"),
    ("GET", "/api/season/leaderboard?tab=" + "x" * 5000, 422, "invalid_tab"),
    ("GET", "/api/season/leaderboard?tab=general&tab=quality", 200, None),
    ("GET", "/api/season/leaderboard?university=" + "u" * 5000, 422, "validation_error"),
    ("GET", "/api/season/leaderboard?season_id=99999999999999999999", 422, "validation_error"),
    ("GET", "/api/season/leaderboard?season_id=abc", 422, "validation_error"),
    ("GET", "/api/season/leaderboard?season_id=-1", 422, "validation_error"),
    ("GET", "/api/season/leaderboard?season_id=999999", 404, "season_not_found"),
    ("GET", "/api/season/leaderboard?university=%00", 422, "validation_error"),
    ("GET", "/api/factory/" + "9" * 5000, 404, "player_not_found"),
], ids=lambda v: v[:50] if isinstance(v, str) else None)
def test_odd_methods_paths_and_queries(api: SmokeApi, method: str, path: str, status: int, code: str | None):
    api.bootstrap()
    with api.unchecked():
        r = api.request(method, path)
    assert not problems(r), problems(r)
    assert r.status_code == status, f"{r.status_code} {r.text[:200]}"
    if code:
        assert code_of(r) == code


@pytest.mark.parametrize("body", [{"cap_hours": 9}, {"cap_hours": -1}, {"cap_hours": "x"}, {"cap_hours": None}, {"reason": "bogus"},
                                  {"reason": 5}, [], "x", 5])
def test_advance_bad_bodies(api: SmokeApi, body):
    api.bootstrap()
    r = api.http.post("/api/advance", json=body)
    assert r.status_code == 422 and code_of(r) == "validation_error", r.text[:200]
    assert api.http.post("/api/advance", content=b"{", headers=J).status_code == 422
    assert api.me()["version"] == 0


@pytest.mark.parametrize("path,kwargs,code", [
    ("/api/quiz/answer", dict(json="x"), "validation_error"),
    ("/api/quiz/answer", dict(json=[]), "validation_error"),
    ("/api/quiz/answer", dict(json=[{"question_id": "q_year", "answer": True}]), "invalid_answer_format"),
    ("/api/quiz/answer", dict(json=[{"question_id": "q_year", "answer": 1.5}]), "invalid_answer_format"),
    ("/api/quiz/answer", dict(json=[{"question_id": "q_year", "answer": 10**30}]), "invalid_answer_format"),
    ("/api/quiz/answer", dict(json=[{"question_id": "q_year", "answer": 0}] * 11), "validation_error"),
    ("/api/quiz/answer", dict(json=[{"question_id": "q_year", "answer": 0}] * 2), "validation_error"),  # duplicate id
    ("/api/quiz/answer", dict(json=[{"questionId": "q\u0000", "answer": 0}]), "validation_error"),
    ("/api/quiz/answer", dict(content=b"\xff\xfe", headers=J), "validation_error"),
    ("/api/events", dict(json={"events": [{"name": "a\u0000"}]}), "validation_error"),
    ("/api/events", dict(json={"events": [{"name": "app_open", "props": {"a": "x\u0000"}}]}), "validation_error"),
    ("/api/events", dict(json={"events": "app_open"}), "validation_error"),
    ("/api/events", dict(json={"events": [{"name": "app_open"}] * 51}), "too_many_events"),
    ("/api/events", dict(content=("[" * 5000 + "]" * 5000).encode(), headers=J), "validation_error"),
    ("/api/events", dict(content=b"\xff\xfe", headers=J), "validation_error"),
    ("/api/leads", dict(json="x"), "validation_error"),
    ("/api/leads", dict(json=lead_body(fullName="A\u0000B")), "validation_error"),
    ("/api/leads", dict(json=lead_body(fullName="A" * 100_000)), "validation_error"),
    ("/api/leads", dict(json=lead_body(course=10**30)), "validation_error"),
    ("/api/leads", dict(json=lead_body(birthDate="0001-01-01")), "validation_error"),
    ("/api/leads", dict(json=lead_body(birthDate="2999-01-01")), "validation_error"),
    ("/api/leads", dict(json=lead_body(phone="+" + "9" * 5000)), "validation_error"),
    ("/api/leads", dict(json=lead_body(email="not-an-email", phone=None, telegram=None)), "validation_error"),
    ("/api/leads", dict(json=lead_body(vacancy="ceo")), "validation_error"),
    ("/api/leads", dict(content=b"{", headers=J), "validation_error"),
])
def test_other_endpoints_bad_bodies(api: SmokeApi, path: str, kwargs: dict, code: str):
    r = api.http.post(path, **kwargs)
    assert r.status_code in (422, 409) and code_of(r) == code, f"{r.status_code} {r.text[:200]}"
    if path == "/api/leads":
        assert api.http.get("/api/leads/me").status_code == 404, "a rejected lead must not be stored"
    if path == "/api/quiz/answer":
        assert api.http.get("/api/quiz/questions").json()["progress"]["answered"] == 0, "answers are all-or-nothing"


@pytest.mark.parametrize("bad, reason", [
    ({"name": "hack"}, "invalid_event_name"), ({"name": "app_open", "ts": -5}, "invalid_ts"),
    ({"name": "app_open", "ts": "garbage"}, "invalid_ts"), ({"name": "app_open", "ts": 10**30}, "invalid_ts"),
    ({"name": "app_open", "props": {"a": "x" * 5000}}, "props_too_large"),
])
def test_events_drop_a_bad_item_and_keep_the_rest(api: SmokeApi, bad: dict, reason: str):
    """S13: the client throws a batch away on any 4xx, so a single bad item is dropped (200 + rejected), not fatal."""
    r = api.http.post("/api/events", json={"events": [ev(), bad]})
    assert r.status_code == 200, r.text
    assert (r.json()["accepted"], r.json()["rejected"], r.json()["rejects"][0]["reason"]) == (1, 1, reason)


@pytest.mark.parametrize("header,status,code", [
    ("garbage", 401, "unauthorized"), ("Bearer abc.def.ghi", 401, "unauthorized"), ("Basic dXNlcjpwdw==", 401, "unauthorized"),
    ("dev", 401, "invalid_init_data"), ("dev abc", 401, "invalid_init_data"), ("dev -3", 401, "invalid_init_data"),
    ("dev 0", 401, "invalid_init_data"), ("dev 1.5", 401, "invalid_init_data"), ("dev 1e5", 401, "invalid_init_data"),
    ("dev :name", 401, "invalid_init_data"), ("dev 9223372036854775807:Max", 200, None),
    ("dev 9223372036854775808", 401, "invalid_init_data"), ("dev 99999999999999999999999", 401, "invalid_init_data"),
])
def test_dev_auth_header_edge_cases(header: str, status: int, code: str | None):
    with httpx.Client(base_url=BASE, verify=False, timeout=30.0) as c:
        r = c.get("/api/me", headers={"Authorization": header})
    assert not problems(r), problems(r)
    assert r.status_code == status, f"{r.status_code} {r.text[:200]}"
    if code:
        assert code_of(r) == code


def test_request_id_is_echoed_or_replaced(api: SmokeApi):
    ok = ["my-trace-123", "a.b_c:d-e", "x" * 128]
    for rid in ok:
        assert api.http.get("/api/me", headers={"X-Request-ID": rid}).headers["x-request-id"] == rid
    for rid in ["has spaces", "x" * 129, "semi;colon", "q" * 5000, "<script>"]:
        r = api.http.get("/api/me", headers={"X-Request-ID": rid})
        assert r.status_code == 200 and r.headers["x-request-id"] != rid and uuid.UUID(r.headers["x-request-id"])
    seen = {api.http.get("/api/me").headers["x-request-id"] for _ in range(5)}
    assert len(seen) == 5, "generated ids are unique"
    err = api.http.get("/api/nope", headers={"X-Request-ID": "err-trace-1"})
    assert err.status_code == 404 and err.headers["x-request-id"] == "err-trace-1"


def test_fuzz_every_command_type_with_poison_never_5xx(new_user):
    """All 27 Command types x (minimal, 3 poisoned variants): validation_error / command_rejected / ok - fast, never a 5xx."""
    schema = json.loads((ROOT / "contract" / "schema" / "command.schema.json").read_text(encoding="utf-8"))
    defs = schema["definitions"]
    types = {}
    for d in defs["Command"]["oneOf"]:
        o = d if "properties" in d else defs[d["$ref"].split("/")[-1]]
        types[o["properties"]["type"]["const"]] = [k for k in o["properties"] if k != "type"]
    assert len(types) >= 27
    poison = [1e30, -1, 0.5, "x" * 3000, None, [], {}, True, "\u0000", [{}] * 50, "../../etc/passwd", {"__proto__": {"x": 1}}]
    rnd = random.Random(7)
    users = [new_user(), new_user()]  # two players: 112 commands would sit right at one player's rate limit
    for u in users:
        u.bootstrap()
    seen, slow = Counter(), []
    n = 0
    for t, fields in types.items():
        for variant in range(4):
            cmd = {"type": t} | ({} if variant == 0 else {f: rnd.choice(poison) for f in fields})
            u = users[n % 2]
            n += 1
            t0 = time.monotonic()
            r = u.send(cmd)
            if time.monotonic() - t0 > 2.5:
                slow.append((t, variant))
            seen[(r.status_code, code_of(r) if r.status_code != 200 else "ok")] += 1
            if r.status_code == 200:
                u.version = r.json()["version"]
            elif r.status_code == 409:
                u.version = u.me()["version"]
            elif r.status_code == 422 and "version" in r.json()["error"]["details"]:
                u.version = r.json()["error"]["details"]["version"]
    assert {s for s, _ in seen} <= {200, 409, 422}, seen
    assert not slow, f"slow poison commands: {slow}"
    for u in users:
        assert u.me()["version"] == u.version and u.me()["degraded"] is False  # the factory is still readable and consistent


# ---- regressions: NUL in command input is a 422, never a 500 (it cannot be stored in the jsonb state) ----
@pytest.mark.parametrize("what", ["key_nul", "name_nul"])
def test_known_bugs_command_input(api: SmokeApi, what: str):
    api.bootstrap()
    cmd = {"type": "renameFactory", "name": "Name OK"}
    key = None
    if what == "key_nul":
        key = "abcdefgh\u0000z"
    elif what == "name_nul":
        cmd["name"] = "A\u0000B"
    with api.unchecked():
        r = api.send(cmd, version=0, key=key)
    assert r.status_code == 422, f"expected 422 validation_error: {r.status_code} {r.text[:120]}"


# ========================================================================================== E. auth
@pytest.mark.parametrize("method,path,body,bucket", ROUTES, ids=ROUTE_IDS)
def test_no_or_bad_auth_is_401_on_every_route(method: str, path: str, body, bucket: str):
    """No header / garbage scheme / malformed dev id / bad initData -> 401 in the unified format; nothing is created."""
    variants = [(None, "unauthorized"), ("garbage", "unauthorized"), ("dev not-a-number", "invalid_init_data"), ("tma bogus-init-data", None)]
    with httpx.Client(base_url=BASE, verify=False, timeout=30.0) as c:
        for header, code in variants:
            for attempt in range(2):  # anonymous traffic shares one per-IP bucket: wait out a 429, never accept it as the answer
                r = c.request(method, path, json=body, headers={"Authorization": header} if header else {})
                if r.status_code != 429:
                    break
                assert not problems(r), problems(r)
                time.sleep(min(int(r.headers.get("Retry-After", "1")), 62) + 0.5)
            assert not problems(r, allow_503=header is not None and header.startswith("tma")), (header, problems(r))
            if header and header.startswith("tma") and r.status_code == 503:  # dev stack without BOT_TOKEN: documented config_error
                assert code_of(r) == "config_error"
                continue
            assert r.status_code == 401, f"{method} {path} [{header}]: {r.status_code} {r.text[:160]}"
            assert code_of(r) == (code or code_of(r)) and code_of(r) in ("unauthorized", "invalid_init_data", "auth_expired")


def test_every_route_with_valid_auth_is_alive(api: SmokeApi):
    """One pass over every route of the README with a valid body: 2xx / documented 4xx, JSON, request id (checked on each)."""
    api.bootstrap()
    expected = {"GET /api/me": {200}, "GET /api/bootstrap": {200}, "POST /api/command": {200}, "POST /api/advance": {200},
                "GET /api/factory/me": {200}, "GET /api/season/leaderboard": {200}, "GET /api/season/summary": {200},
                "GET /api/factory/1": {200, 404}, "POST /api/leads": {201}, "GET /api/leads/me": {200}, "DELETE /api/leads/me": {204},
                "GET /api/quiz/questions": {200}, "POST /api/quiz/answer": {200}, "POST /api/events": {200}}
    for (method, path, body, _), rid in zip(ROUTES, ROUTE_IDS):
        if method == "POST" and path == "/api/command":
            body = {**body, "version": api.version, "idempotency_key": uuid.uuid4().hex}
        r = api.http.request(method, path, json=body)
        assert r.status_code in expected[rid], f"{rid}: {r.status_code} {r.text[:160]}"
    assert api.http.get("/health").json()["status"] == "ok"
    assert api.http.get("/ready").json()["status"] == "ready"


def test_player_data_is_isolated_between_users(new_user):
    a, b = new_user(), new_user()
    a.bootstrap()
    b.bootstrap()
    assert a.http.post("/api/leads", json=lead_body()).status_code == 201
    first = [{"question_id": i, "answer": CORRECT.get(i, 0)} for i in [q["id"] for q in a.http.get("/api/quiz/questions").json()["questions"]][:3]]
    assert a.http.post("/api/quiz/answer", json=first).status_code == 200
    assert b.http.get("/api/leads/me").status_code == 404 and b.http.delete("/api/leads/me").status_code == 404
    assert b.http.get("/api/quiz/questions").json()["progress"]["answered"] == 0
    assert b.http.get("/api/season/summary").json()["leadStatus"] is None
    assert a.http.get("/api/leads/me").status_code == 200  # and B's attempts did not touch A's lead
    pa = a.me()["snapshot"]["player"]["id"]
    pub = b.http.get(f"/api/factory/{pa}")
    assert pub.status_code == 200 and "leads" not in pub.text and "phone" not in pub.text and "tgId" not in pub.text


# ========================================================================================== F. every response
def test_gzip_when_the_client_accepts_it_and_the_body_is_large(api: SmokeApi):
    api.bootstrap()
    for i in range(6):
        api.command(belt(i, 2))
    plain = api.http.get("/api/factory/me", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and len(plain.content) > 1000
    for path in ("/api/factory/me", "/api/bootstrap", "/api/season/leaderboard?tab=general"):
        gz = api.http.get(path, headers={"Accept-Encoding": "gzip"})
        assert gz.status_code == 200 and gz.headers.get("content-encoding") == "gzip", path
        assert "accept-encoding" in gz.headers.get("vary", "").lower()
        assert gz.num_bytes_downloaded < len(gz.content) * 0.8, f"{path}: {gz.num_bytes_downloaded} wire vs {len(gz.content)} decoded"
        assert gz.headers["content-type"].startswith("application/json")
    gz = api.http.get("/api/factory/me", headers={"Accept-Encoding": "gzip"}).json()
    assert plan(gz["snapshot"]) == plan(plain.json()["snapshot"])  # same document either way
    br = api.http.get("/api/factory/me", headers={"Accept-Encoding": "gzip, deflate, br"})
    assert br.status_code == 200 and br.headers.get("content-encoding") == "gzip"
    health = api.http.get("/health", headers={"Accept-Encoding": "gzip"})
    assert health.status_code == 200 and "content-encoding" not in health.headers  # tiny bodies are not worth compressing
    small_err = api.http.get("/api/leads/me", headers={"Accept-Encoding": "gzip"})
    assert small_err.status_code == 404 and small_err.json()["error"]["code"] == "lead_not_found"


def test_health_and_ready_are_public_and_clean():
    with httpx.Client(base_url=BASE, verify=False, timeout=10.0) as c:
        for path in ("/health", "/ready"):
            r = c.get(path)
            assert r.status_code == 200 and not problems(r), (path, problems(r))
        assert c.get("/ready").json()["checks"]["db"] == "ok"
        assert c.get("/internal/seasons/rotate").status_code == 404  # never reachable from outside
        assert c.post("/internal/seasons/rotate", headers={"X-Internal-Token": "x"}).status_code == 404


# ========================================================================================== G. degraded (optional)
@DOCKER
def test_sim_outage_mixed_traffic_and_recovery(new_user):
    """E2E_DOCKER=1. Sim container stopped: reads from storage + leads/quiz/events/leaderboard work, game writes are clean 503s
    (circuit breaker), a brand-new player's bootstrap is a clean 503 without leaving a half-made factory; after `start sim` everything
    recovers by itself. (The restart / degraded details are in test_s7_resilience.py and test_s5_*.py; this is the mixed-traffic view.)"""
    a, fresh = new_user(allow_503=True), new_user(allow_503=True)
    a.bootstrap()
    a.command(belt(0, 2))
    key = uuid.uuid4().hex
    assert a.send(belt(1, 2), version=a.version, key=key).status_code == 200
    a.version += 1
    try:
        compose("stop", "sim")
        me = a.me()
        assert me["degraded"] is True and me["version"] == a.version and tiles(me["snapshot"]) == 2
        assert a.http.get("/api/season/leaderboard").status_code == 200 and a.http.get("/api/season/summary").status_code == 200
        assert a.http.post("/api/leads", json=lead_body()).status_code == 201
        assert a.http.get("/api/leads/me").status_code == 200
        assert a.http.get("/api/quiz/questions").status_code == 200
        assert a.http.post("/api/quiz/answer", json=[{"question_id": "q_year", "answer": 1}]).status_code == 200
        assert a.http.post("/api/events", json={"events": [ev()]}).status_code == 200
        assert a.http.get("/health").status_code == 200
        codes = Counter()
        for i in range(16):  # the first failures open the breaker (per worker); the rest fail fast with details.reason=circuit_open
            r = a.send(belt(2 + i % 4, 2), version=a.version)
            assert r.status_code == 503 and code_of(r) == "sim_unavailable", r.text[:200]
            codes[r.json()["error"]["details"].get("reason")] += 1
            if r.json()["error"]["details"].get("reason") == "circuit_open":
                assert int(r.headers["Retry-After"]) >= 1
        assert codes["circuit_open"] >= 1, codes
        replay = a.send(belt(1, 2), version=0, key=key)  # an already applied command resent during the outage
        assert replay.status_code in (200, 503)
        r = fresh.http.get("/api/bootstrap")
        assert r.status_code == 503 and code_of(r) == "sim_unavailable"
        assert fresh.http.get("/api/me").status_code == 200 and fresh.http.get("/api/quiz/questions").status_code == 200
    finally:
        compose("start", "sim")
        wait_sim_healthy()
        wait_ready()
    deadline = time.monotonic() + 45
    while a.me()["degraded"]:  # breaker: open 10 s, then one half-open probe
        assert time.monotonic() < deadline, "factory/me still degraded 45 s after the sim is back"
        time.sleep(2)
    a.version = a.me()["version"]
    assert a.play(belt(2, 2)).ok and tiles(a.me()["snapshot"]) == 3, "the outage lost or doubled nothing"
    boot = fresh.bootstrap()  # a clean first factory: the failed bootstrap left nothing behind
    assert boot["version"] == 0 and credits_of(boot["snapshot"]) == START_CREDITS and layout_of(boot["snapshot"])["buildings"] == []
    assert a.http.get("/api/leads/me").status_code == 200 and a.http.get("/api/season/summary").json()["leadStatus"] == "new"
