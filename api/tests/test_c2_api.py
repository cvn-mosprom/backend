"""C2 on a real Postgres + Redis (skipped when absent): competency / fit written on every evaluation, GET /api/achievements/me and
/catalog (shape vs the frontend types, hidden masking, fit never leaked), `achievementsUnlocked` on /api/command and on replays, the
`achievements` leaderboard tab, ОД and top achievements in the season summary / factory visit, and the outbox + lead_events rows that
are written in the same transaction as the grant. In-process fake sim (tests/fake_sim.py)."""
from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from app.achievements import catalog as cat
from app.achievements import scores as sc
from app.achievements import service
from tests.game_helpers import bootstrap, command, key, place, shift
from tests.helpers import INTERNAL_TOKEN, TEST_DB_URL, TEST_REDIS_URL, dev

SKUS5 = {k: 1_000 + i for i, k in enumerate(cat.SKU_IDS[:5])}
TUTORIAL = ["q_intro", "q_brew", "q_bottles", "q_filler", "q_line", "q_dock"]
# first_line 10 + first_order 10 + bottles_1k 10 + sku_5 20 + no_defects 30 = 80
RICH = {"sim": {"produced": 1500, "qualitySum": 1500 * 0.99, "collection": SKUS5, "orders": [], "layout": {"buildings": []}},
        "dialogs": {"done": TUTORIAL, "factsSeen": [], "lastHintAt": 0}}
RICH_IDS = ["first_line", "first_order", "bottles_1k", "sku_5", "no_defects"]  # catalog order
RICH_POINTS = 80
SMALL = {"dialogs": {"done": TUTORIAL, "factsSeen": [], "lastHintAt": 0}}  # first_line + first_order = 20
FRONT_ENTRY = {"id", "title", "description", "category", "tier", "points", "competency", "hidden", "seasonal", "target", "profession"}


@pytest_asyncio.fixture
async def c(make_client, clean_db, clean_redis, fake_sim):
    client = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                               rate_limit_enabled=False, internal_token=INTERNAL_TOKEN)
    client.db, client.redis, client.sim = clean_db, clean_redis, fake_sim  # type: ignore[attr-defined]
    client.sm = client.app.state.sessionmaker  # type: ignore[attr-defined]
    return client


async def mk(c, tg: int, name: str | None = None, state: dict | None = None) -> int:
    await bootstrap(c, tg, name)
    uid = await c.db.fetchval("SELECT id FROM users WHERE tg_id = $1", tg)
    if state:
        await c.db.execute("UPDATE factories SET state = state || $1::jsonb WHERE user_id = $2", json.dumps(state), uid)
    return uid


async def hit(c, tg: int, state: dict, version: int = 0, idem: int | None = None) -> Any:
    """A player with `state`, then one command (the hook grants what the state earns)."""
    uid = await mk(c, tg, state=state)
    r = await command(c, version, key(idem or tg), place(), tg=tg)
    assert r.status_code == 200, r.text
    return uid, r


def keys_of(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            out |= keys_of(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= keys_of(v)
    return out


async def score_row(c, uid: int) -> dict[str, Any]:
    row = await c.db.fetchrow("SELECT * FROM player_scores WHERE user_id = $1", uid)
    assert row is not None
    return {**dict(row), "competency": json.loads(row["competency"]), "fit": json.loads(row["fit"])}


async def expected_scores(c, uid: int) -> tuple[dict, dict]:
    """Recompute competency / fit from the tables the way the service does, to compare with what was stored."""
    state = json.loads(await c.db.fetchval("SELECT state FROM factories WHERE user_id = $1", uid))
    earned = {r["achievement_id"] for r in await c.db.fetch("SELECT achievement_id FROM achievements_earned WHERE user_id = $1", uid)}
    prog: dict[str, float] = {}
    for r in await c.db.fetch("SELECT achievement_id, current FROM achievement_progress WHERE user_id = $1 AND achievement_id <> '_counters'", uid):
        prog[r["achievement_id"]] = max(prog.get(r["achievement_id"], 0.0), r["current"])
    table = {r["profession"]: {"played": r["n"], "wins": r["w"], "avg_score": r["a"]} for r in await c.db.fetch(
        "SELECT profession, count(*) n, count(*) FILTER (WHERE result = 'win') w, avg(score)::float8 a FROM shifts "
        "WHERE user_id = $1 GROUP BY profession", uid)}
    from app.achievements.engine import professions
    return sc.score_player(state, professions(state, table), earned, prog)


# ---------------------------------------------------------------- competency / fit are written on every evaluation

async def test_the_hook_writes_competency_and_fit_when_something_unlocks(c):
    uid, _ = await hit(c, 1, RICH)
    row = await score_row(c, uid)
    comp, fit = await expected_scores(c, uid)
    assert row["competency"] == comp and row["fit"] == fit
    assert row["competency"]["reliability"] > 0 and set(row["competency"]) == set(cat.COMPETENCIES)
    assert set(row["fit"]) == set(cat.PROFESSIONS)
    for f in row["fit"].values():
        assert sum(r["delta"] for r in f["reasons"]) == f["score"] and set(f) == {"score", "reasons"}


async def test_a_command_that_unlocks_nothing_writes_nothing(c):
    uid, _ = await hit(c, 1, RICH)
    before = await score_row(c, uid)
    assert (await command(c, 1, key(50), place(2, 2))).status_code == 200  # nothing new, not a shift: the cheap hook stays cheap
    assert await score_row(c, uid) == before


async def test_a_shift_recomputes_the_scores_from_the_new_skills(c):
    uid = await mk(c, 1)
    await c.db.execute("UPDATE factories SET state = jsonb_set(state, '{skills}', $1::jsonb) WHERE user_id = $2",
                       json.dumps({"speed": 80, "accuracy": 70, "attention": 60, "endurance": 50, "byProfession": {}}), uid)
    assert (await command(c, 0, key(1), shift(score=0.8))).status_code == 200
    row = await score_row(c, uid)
    comp, fit = await expected_scores(c, uid)
    assert row["competency"] == comp and row["fit"] == fit
    assert row["competency"]["speed"] == round(0.75 * 80 * 0.1)  # one shift: confidence 10%
    assert row["fit"]["lab"]["score"] > 0  # the shift counts for «Лаборант ОТК» (profession of the shift)
    assert row["fit"]["lab"]["score"] >= max(f["score"] for p, f in row["fit"].items() if p != "lab") - 5
    sid = await c.db.fetchval("SELECT max(id) FROM seasons")
    for day in range(10):  # ten more shifts on earlier days (the daily limit allows five per day)
        await c.db.execute("INSERT INTO shifts (user_id, season_id, building_id, profession, game_id, result, score, duration_ms, created_at) "
                           "VALUES ($1, $2, 'b1', 'lab', 'g1', 'win', 0.8, 5000, now() - make_interval(days => $3))", uid, sid, day + 2)
    assert (await command(c, 1, key(2), shift(score=0.8))).status_code == 200  # the 12th: the hook sees all of them
    again = await score_row(c, uid)
    assert again["competency"]["speed"] == round(0.75 * 80)  # 12 shifts: full confidence
    assert again["fit"]["lab"]["score"] > row["fit"]["lab"]["score"]
    comp, fit = await expected_scores(c, uid)
    assert again["competency"] == comp and again["fit"] == fit


async def test_recompute_fills_the_scores_and_a_backfill_is_idempotent(c):
    uid = await mk(c, 1, state=RICH)
    await c.db.execute("UPDATE factories SET state = jsonb_set(state, '{skills}', $1::jsonb) WHERE user_id = $2",
                       json.dumps({"speed": 90, "accuracy": 90, "attention": 90, "endurance": 90, "byProfession": {}}), uid)
    await c.db.execute("UPDATE factories SET state = jsonb_set(state, '{shiftsTotal}', '20') WHERE user_id = $1", uid)
    res = await service.recompute(c.sm, force=True)
    assert res["evaluated"] == 1
    row = await score_row(c, uid)
    comp, fit = await expected_scores(c, uid)
    assert row["competency"] == comp and row["fit"] == fit and row["evaluated_at"] is not None
    assert row["competency"]["speed"] == round(0.75 * 90)
    await service.recompute(c.sm, force=True)
    again = await score_row(c, uid)
    assert {k: again[k] for k in ("competency", "fit", "achievement_points", "season_points")} == {
        k: row[k] for k in ("competency", "fit", "achievement_points", "season_points")}


async def test_dry_run_leaves_no_scores_no_outbox_no_events(c):
    uid = await mk(c, 1, state=RICH)
    await service.recompute(c.sm, force=True, dry_run=True)
    assert await c.db.fetchval("SELECT count(*) FROM player_scores") == 0
    assert await c.db.fetchval("SELECT count(*) FROM outbox WHERE kind = 'achievement_unlocked'") == 0
    assert await c.db.fetchval("SELECT count(*) FROM achievements_earned WHERE user_id = $1", uid) == 0


# ---------------------------------------------------------------- GET /api/achievements/me

async def test_me_requires_auth(c):
    assert (await c.get("/api/achievements/me")).status_code == 401
    assert (await c.get("/api/achievements/catalog")).status_code == 401


async def test_me_shape_matches_the_frontend_types_and_hides_nothing_earned(c):
    uid, _ = await hit(c, 1, RICH)
    r = await c.get("/api/achievements/me", headers=dev(1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["achievementPoints"] == RICH_POINTS and body["seasonPoints"] == RICH_POINTS
    assert body["earnedCount"] == 5 and body["total"] == len(cat.CATALOG) and body["catalogVersion"] == cat.CATALOG_VERSION
    assert set(body["competency"]) == set(cat.COMPETENCIES) and all(isinstance(v, int) for v in body["competency"].values())
    assert body["competency"] == (await score_row(c, uid))["competency"]
    assert [i["id"] for i in body["items"]] == [a.id for a in cat.CATALOG]  # the whole catalog, catalog order
    for item in body["items"]:
        assert FRONT_ENTRY <= set(item) and {"earned", "earnedAt", "times", "progress"} <= set(item)
    got = {i["id"]: i for i in body["items"]}
    assert got["bottles_1k"]["earned"] is True and isinstance(got["bottles_1k"]["earnedAt"], int) and got["bottles_1k"]["times"] == 1
    assert got["bottles_1k"]["progress"] == {"current": 1000, "target": 1000}
    assert got["bottles_10k"]["earned"] is False and got["bottles_10k"]["earnedAt"] is None
    assert got["bottles_10k"]["progress"] == {"current": 1500, "target": 10000}
    assert got["sku_all"]["progress"] == {"current": 5, "target": 8}
    assert got["season_top10"]["progress"] is None  # a placement: no bar
    assert [e["id"] for e in sorted(body["earned"], key=lambda e: RICH_IDS.index(e["id"]))] == RICH_IDS
    assert all(set(e) == {"id", "title", "tier", "points", "earnedAt", "seasonId", "seasonal"} for e in body["earned"])
    assert body["earned"][0]["earnedAt"] >= body["earned"][-1]["earnedAt"]  # newest first


async def test_hidden_achievements_are_masked_until_earned(c):
    uid = await mk(c, 1)
    body = (await c.get("/api/achievements/me", headers=dev(1))).json()
    hidden = next(i for i in body["items"] if i["id"] == "perfect_shift")
    assert hidden["hidden"] is True and hidden["title"] == "???" and hidden["earned"] is False
    assert hidden["competency"] is None and hidden["target"] is None and hidden["progress"] is None and hidden["profession"] is None
    assert "99" not in json.dumps(hidden, ensure_ascii=False) and cat.BY_ID["perfect_shift"].description not in json.dumps(hidden, ensure_ascii=False)
    assert (await command(c, 0, key(1), shift(score=1.0, result="win"))).status_code == 200  # the hidden one, from the shift hook
    body = (await c.get("/api/achievements/me", headers=dev(1))).json()
    shown = next(i for i in body["items"] if i["id"] == "perfect_shift")
    assert shown["earned"] is True and shown["title"] == cat.BY_ID["perfect_shift"].title and shown["description"] == cat.BY_ID["perfect_shift"].description
    assert shown["competency"] == "accuracy" and shown["progress"] == {"current": 1, "target": 1}


async def test_seasonal_achievement_reports_the_current_season_bar(c):
    uid = await mk(c, 1)
    sid = await c.db.fetchval("SELECT max(id) FROM seasons")
    for i in range(7):
        await c.db.execute("INSERT INTO shifts (user_id, season_id, building_id, profession, game_id, result, score, duration_ms) "
                           "VALUES ($1, $2, 'b1', 'lab', 'g1', 'win', 0.8, 5000)", uid, sid)
    await service.recompute(c.sm, force=True)
    body = (await c.get("/api/achievements/me", headers=dev(1))).json()
    reg = next(i for i in body["items"] if i["id"] == "season_regular")
    assert reg["seasonal"] is True and reg["earned"] is False and reg["progress"] == {"current": 7, "target": 15}


async def test_fit_is_never_exposed_to_players(c):
    uid, _ = await hit(c, 1, RICH)
    await hit(c, 2, SMALL)
    assert "fit" in (await score_row(c, uid))  # it is stored ...
    for path in ("/api/achievements/me", "/api/achievements/catalog", "/api/bootstrap", "/api/factory/me", "/api/season/summary",
                 f"/api/factory/{uid}", "/api/season/leaderboard?tab=achievements", "/api/season/leaderboard?tab=general"):
        r = await c.get(path, headers=dev(1))
        assert r.status_code == 200, (path, r.text)
        flat = keys_of(r.json())
        assert not {"fit", "fitBest", "reasons"} & flat, path  # ... but no player endpoint carries it
    assert "competency" in keys_of((await c.get("/api/achievements/me", headers=dev(1))).json())


# ---------------------------------------------------------------- GET /api/achievements/catalog

async def test_catalog_lists_everything_for_any_user_and_masks_hidden_ones_for_non_hr(c):
    await mk(c, 1)
    r = await c.get("/api/achievements/catalog", headers=dev(1))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == [a.id for a in cat.CATALOG]
    assert all(set(i) == {"id", "title", "tier", "points", "category", "competency", "hidden"} for i in items)
    one = next(i for i in items if i["id"] == "no_defects")
    assert one == {"id": "no_defects", "title": "Без брака", "tier": "gold", "points": 30, "category": "quality",
                   "competency": "attention", "hidden": False}
    masked = next(i for i in items if i["id"] == "perfect_shift")
    assert masked["title"] == "???" and masked["hidden"] is True and masked["competency"] is None
    await c.db.execute("INSERT INTO hr_users (tg_id, role, active) VALUES (1, 'analyst', true)")
    full = next(i for i in (await c.get("/api/achievements/catalog", headers=dev(1))).json()["items"] if i["id"] == "perfect_shift")
    assert full["title"] == cat.BY_ID["perfect_shift"].title and full["competency"] == "accuracy"
    await c.db.execute("UPDATE hr_users SET active = false")  # a deactivated HR user is just a player again
    again = next(i for i in (await c.get("/api/achievements/catalog", headers=dev(1))).json()["items"] if i["id"] == "perfect_shift")
    assert again["title"] == "???"


# ---------------------------------------------------------------- achievementsUnlocked on /api/command

async def test_command_carries_achievements_unlocked_on_top_level_and_on_replay(c):
    uid = await mk(c, 1, state=RICH)
    r = await command(c, 0, key(1), place())
    assert r.status_code == 200
    body = r.json()
    ids = [a["id"] for a in body["achievementsUnlocked"]]
    assert ids == RICH_IDS and body["replayed"] is False
    first = body["achievementsUnlocked"][0]
    assert set(first) == {"id", "title", "description", "category", "tier", "points", "hidden", "seasonal"}
    assert {"id", "title", "points", "tier"} <= set(first)  # what the frontend toast reads
    assert "achievementsUnlocked" not in body["result"]  # top level only
    # replay through the Redis fast path ...
    again = await command(c, 0, key(1), place())
    assert again.status_code == 200 and again.json()["replayed"] is True and again.json()["achievementsUnlocked"] == body["achievementsUnlocked"]
    # ... and through the database (Redis forgot the key)
    await c.redis.flushdb()
    third = await command(c, 5, key(1), place())  # an old version is fine for a replay
    assert third.status_code == 200 and third.json()["replayed"] is True
    assert third.json()["achievementsUnlocked"] == body["achievementsUnlocked"]
    # a different command that unlocks nothing says so, and its replay too
    nothing = await command(c, 1, key(2), place(2, 2))
    assert nothing.json()["achievementsUnlocked"] == []
    assert (await command(c, 1, key(2), place(2, 2))).json()["achievementsUnlocked"] == []
    assert await c.db.fetchval("SELECT count(*) FROM achievements_earned WHERE user_id = $1", uid) == 5  # granted once


async def test_a_shift_command_lists_the_new_hidden_achievement(c):
    await mk(c, 1)
    body = (await command(c, 0, key(1), shift(score=1.0, result="win"))).json()
    assert [a["id"] for a in body["achievementsUnlocked"]] == ["perfect_shift"] and body["achievementsUnlocked"][0]["hidden"] is True
    await c.redis.flushdb()
    replay = (await command(c, 0, key(1), shift(score=1.0, result="win"))).json()
    assert replay["replayed"] is True and [a["id"] for a in replay["achievementsUnlocked"]] == ["perfect_shift"]


async def test_the_old_client_ignores_the_new_field(c):
    """HttpGameApi.command reads toast / rewards / replayed only, so an extra top-level key cannot hurt; the old keys are intact."""
    await mk(c, 1, state=RICH)
    body = (await command(c, 0, key(1), place())).json()
    assert {"ok", "version", "result", "events", "snapshot", "replayed", "toast"} <= set(body)
    assert body["toast"] == body["result"]["toast"]


# ---------------------------------------------------------------- leaderboard tab `achievements`

async def test_achievements_tab_ranks_by_points_and_leaves_bots_and_zero_out(c):
    a, _ = await hit(c, 1, RICH)  # 80
    b, _ = await hit(c, 2, SMALL)  # 20
    z = await mk(c, 3)  # nothing
    r = await c.get("/api/season/leaderboard?tab=achievements", headers=dev(1))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tab"] == "achievements" and body["total"] == 2
    assert [(row["playerId"], row["value"], row["place"]) for row in body["rows"]] == [(str(a), 80, 1), (str(b), 20, 2)]
    assert body["rows"][0]["isMe"] is True and body["me"]["place"] == 1 and body["me"]["value"] == 80
    nobody = (await c.get("/api/season/leaderboard?tab=achievements", headers=dev(3))).json()
    assert "me" not in nobody and nobody["total"] == 2  # no points: not on the board
    other = (await c.get("/api/season/leaderboard?tab=achievements", headers=dev(2))).json()
    assert other["me"]["place"] == 2 and other["me"]["value"] == 20 and other["total"] == 2
    # a demo bot never earns points, so it is never on this board (the general tab lists it)
    bot = await mk(c, 4, state=RICH)
    await c.db.execute("UPDATE users SET is_bot = true WHERE id = $1", bot)
    await c.db.execute("UPDATE factories SET is_bot = true WHERE user_id = $1", bot)
    assert (await command(c, 0, key(4), place(), tg=4)).status_code == 200
    await c.redis.flushdb()
    rows = (await c.get("/api/season/leaderboard?tab=achievements", headers=dev(1))).json()["rows"]
    assert [row["playerId"] for row in rows] == [str(a), str(b)]
    _ = z


async def test_achievements_tab_scope_season_uses_season_points(c):
    a, _ = await hit(c, 1, RICH)
    b, _ = await hit(c, 2, SMALL)
    # b earned its 20 points in an earlier season: lifetime points stay, season points go
    await c.db.execute("UPDATE achievements_earned SET earned_season_id = NULL WHERE user_id = $1", b)
    await service.recompute(c.sm, force=True)
    allb = (await c.get("/api/season/leaderboard?tab=achievements", headers=dev(2))).json()
    seasonb = (await c.get("/api/season/leaderboard?tab=achievements&scope=season", headers=dev(2))).json()
    assert [row["value"] for row in allb["rows"]] == [80, 20] and allb["me"]["value"] == 20
    assert [row["value"] for row in seasonb["rows"]] == [80] and "me" not in seasonb and seasonb["total"] == 1
    assert (await c.get("/api/season/leaderboard?tab=achievements&scope=season", headers=dev(1))).json()["me"]["place"] == 1
    _ = a


async def test_achievements_tab_caches_per_scope_and_validates(c):
    await hit(c, 1, RICH)
    first = (await c.get("/api/season/leaderboard?tab=achievements", headers=dev(1))).json()
    keys = [k async for k in c.redis.scan_iter("lb:*")]
    assert any(k.endswith(":achievements:" + ":1:all") or ":achievements:" in k and k.endswith(":all") for k in keys), keys
    assert (await c.get("/api/season/leaderboard?tab=achievements", headers=dev(1))).json() == first
    r = await c.get("/api/season/leaderboard?tab=achievements&scope=forever", headers=dev(1))
    assert r.status_code == 422
    bad = await c.get("/api/season/leaderboard?tab=nope", headers=dev(1))
    assert bad.status_code == 422 and "achievements" in bad.json()["error"]["details"]["allowed"]
    for tab in ("general", "quality", "speed", "uni"):  # the old tabs are untouched
        assert (await c.get(f"/api/season/leaderboard?tab={tab}", headers=dev(1))).status_code == 200


# ---------------------------------------------------------------- ОД and top achievements in summary / visit

async def test_summary_and_visit_carry_points_and_the_three_best_achievements(c):
    uid, _ = await hit(c, 1, RICH)
    s = (await c.get("/api/season/summary", headers=dev(1))).json()
    assert s["achievementPoints"] == RICH_POINTS and s["seasonPoints"] == RICH_POINTS
    # most points first: no_defects 30, sku_5 20, then a 10-point one (the latest earned of the three: catalog order is the insert order)
    assert [t["id"] for t in s["topAchievements"][:2]] == ["no_defects", "sku_5"] and len(s["topAchievements"]) == 3
    assert all(set(t) == {"id", "title", "tier", "points"} for t in s["topAchievements"])
    assert s["topAchievements"][0] == {"id": "no_defects", "title": "Без брака", "tier": "gold", "points": 30}
    v = (await c.get(f"/api/factory/{uid}", headers=dev(2))).json()
    assert v["achievementPoints"] == RICH_POINTS and v["topAchievements"] == s["topAchievements"]
    assert {"player", "factory"} <= set(v)
    fresh = await mk(c, 4, state={"sim": {"layout": {"buildings": []}}})
    empty = (await c.get(f"/api/factory/{fresh}", headers=dev(1))).json()
    assert empty["achievementPoints"] == 0 and empty["topAchievements"] == []


# ---------------------------------------------------------------- outbox + lead_events in the grant's transaction

async def add_lead(c, uid: int, revoked: bool = False) -> int:
    sid = await c.db.fetchval("SELECT max(id) FROM seasons")
    return await c.db.fetchval(
        "INSERT INTO leads (user_id, season_id, full_name, university, consent, consent_at, consent_text_version, consent_revoked_at) "
        "VALUES ($1, $2, 'Иван', 'МАИ', true, now(), 'v1', CASE WHEN $3 THEN now() END) RETURNING id", uid, sid, revoked)


async def test_outbox_and_lead_events_are_written_with_the_grant(c):
    uid = await mk(c, 1, state=RICH)
    lead = await add_lead(c, uid)
    other = await mk(c, 2, state=SMALL)  # no lead: outbox yes, lead_events no
    assert (await command(c, 0, key(1), place())).status_code == 200
    assert (await command(c, 0, key(2), place(), tg=2)).status_code == 200
    rows = await c.db.fetch("SELECT payload FROM outbox WHERE kind = 'achievement_unlocked' ORDER BY id")
    pl = [json.loads(r["payload"]) for r in rows]
    assert len(pl) == 5 + 2
    mine = [p for p in pl if p["user_id"] == uid]
    assert [p["achievement_id"] for p in mine] == RICH_IDS
    sku = next(p for p in mine if p["achievement_id"] == "sku_5")
    assert sku == {"user_id": uid, "achievement_id": "sku_5", "title": "Пять напитков", "tier": "silver", "points": 20,
                   "seasonal": False, "season_id": await c.db.fetchval("SELECT max(id) FROM seasons"), "via": "hook"}
    assert {p["user_id"] for p in pl} == {uid, other}
    ev = await c.db.fetch("SELECT lead_id, kind, actor_hr_id, payload FROM lead_events ORDER BY id")
    assert len(ev) == 5 and {e["lead_id"] for e in ev} == {lead} and {e["kind"] for e in ev} == {"achievement"}
    assert all(e["actor_hr_id"] is None for e in ev)  # written by the system
    assert [json.loads(e["payload"]) for e in ev][2] == {"id": "bottles_1k", "title": "Тысяча бутылок", "points": 10, "tier": "bronze"}
    assert all(set(json.loads(e["payload"])) == {"id", "title", "points", "tier"} for e in ev)
    # nothing new -> nothing more
    assert (await command(c, 1, key(3), place(2, 2))).status_code == 200
    await service.recompute(c.sm, force=True)
    assert await c.db.fetchval("SELECT count(*) FROM outbox WHERE kind = 'achievement_unlocked'") == 7
    assert await c.db.fetchval("SELECT count(*) FROM lead_events") == 5


async def test_a_revoked_lead_gets_no_history(c):
    uid = await mk(c, 1, state=RICH)
    await add_lead(c, uid, revoked=True)  # 152-FZ: hidden from the CRM, no new events
    assert (await command(c, 0, key(1), place())).status_code == 200
    assert await c.db.fetchval("SELECT count(*) FROM lead_events") == 0
    assert await c.db.fetchval("SELECT count(*) FROM outbox WHERE kind = 'achievement_unlocked'") == 5


async def test_the_recompute_announces_too_but_a_backfill_does_not_message_anyone(c):
    uid = await mk(c, 1)
    lead = await add_lead(c, uid)
    await c.db.execute("UPDATE factories SET state = state || $1::jsonb WHERE user_id = $2", json.dumps(RICH), uid)
    from app.achievements import backfill
    out = await backfill.run(c.sm, batch=50)
    assert out["granted"] == 5
    assert await c.db.fetchval("SELECT count(*) FROM outbox WHERE kind = 'achievement_unlocked'") == 0  # no mass notifications
    assert await c.db.fetchval("SELECT count(*) FROM lead_events WHERE lead_id = $1", lead) == 5  # the history still gets them
    b = await mk(c, 2, state=RICH)
    res = await service.recompute(c.sm)  # the bot's 10-minute job: a normal grant
    assert res["granted"] >= 5
    via = {json.loads(r["payload"])["via"] for r in await c.db.fetch("SELECT payload FROM outbox WHERE kind = 'achievement_unlocked'")}
    assert via == {"recompute"} and await c.db.fetchval("SELECT count(DISTINCT payload->>'user_id') FROM outbox") == 1
    _ = b


async def test_seasonal_outbox_rows_say_so(c):
    uid = await mk(c, 1)
    sid = await c.db.fetchval("SELECT max(id) FROM seasons")
    await c.db.execute("INSERT INTO season_results (season_id, user_id, place, stars, is_bot) VALUES ($1, $2, 2, 100, false)", sid, uid)
    await service.recompute(c.sm, force=True)
    rows = [json.loads(r["payload"]) for r in await c.db.fetch("SELECT payload FROM outbox WHERE kind = 'achievement_unlocked'")]
    got = {p["achievement_id"]: p for p in rows}
    assert set(got) == {"season_top50", "season_top10", "season_top3"}
    assert all(p["seasonal"] is True for p in got.values()) and got["season_top3"]["points"] == 100 and got["season_top3"]["tier"] == "gold"


async def test_atomic_with_the_grant_a_failing_announce_takes_the_grant_back(c, monkeypatch):
    uid = await mk(c, 1, state=RICH)
    await add_lead(c, uid)

    async def boom(*a, **kw):
        raise RuntimeError("outbox insert fails")

    monkeypatch.setattr(service, "_announce", boom)
    r = await command(c, 0, key(1), place())  # the hook logs and rolls back to its savepoint: the command itself goes through
    assert r.status_code == 200 and r.json()["achievementsUnlocked"] == []
    assert await c.db.fetchval("SELECT count(*) FROM achievements_earned") == 0
    assert await c.db.fetchval("SELECT count(*) FROM outbox WHERE kind = 'achievement_unlocked'") == 0
    assert await c.db.fetchval("SELECT count(*) FROM lead_events") == 0
    assert await c.db.fetchval("SELECT count(*) FROM player_scores") == 0
    assert await c.db.fetchval("SELECT version FROM factories WHERE user_id = $1", uid) == 1


async def test_bots_and_test_users_get_no_outbox_rows(c):
    real = await mk(c, 1, state=RICH)
    await mk(c, 2, "e2e-player", state=RICH)
    bot = await mk(c, 3, state=RICH)
    await c.db.execute("UPDATE users SET is_bot = true WHERE id = $1", bot)
    for tg in (1, 2, 3):
        assert (await command(c, 0, key(tg), place(), tg=tg)).status_code == 200
    await service.recompute(c.sm, force=True)
    users = {json.loads(r["payload"])["user_id"] for r in await c.db.fetch("SELECT payload FROM outbox WHERE kind = 'achievement_unlocked'")}
    assert users == {real}
