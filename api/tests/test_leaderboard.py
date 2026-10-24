"""S4: GET /api/season/leaderboard, GET /api/factory/{playerId}, GET /api/season/summary, snapshot.rank, speed/quality
denormalisation, bot seed. Real Postgres + Redis (skipped when absent); boards are built by hand with raw SQL so that every
number in the assertions can be checked on paper."""
from __future__ import annotations

import json

import pytest

from app.game import SPEED_WINDOW_MS, denormalise, measure_speed
from app.leaderboard import TABS, best_profession
from app.seed_bots import BOT_TG_BASE, bot_values, gen_bot, grow_bots, seed
from tests.game_helpers import advance, bootstrap, command, factory_row, key, me, shift
from tests.helpers import DeadRedis, TEST_DB_URL, TEST_REDIS_URL, dev, tma
from tests.test_errors import assert_error

H = 3_600_000
LAYOUT = {"hangar": {"w": 24, "h": 14, "paint": []}, "buildings": [{"id": "b1", "kind": "filler", "x": 1, "y": 1}],
          "conveyors": []}


# ---------------------------------------------------------------- hand-built boards

async def mk_season(db, closed: bool = False) -> int:
    return await db.fetchval("INSERT INTO seasons (starts_at, ends_at, days, closed) "
                             "VALUES (now(), now() + interval '7 days', 7, $1) RETURNING id", closed)


async def mk_player(db, season_id: int, tg: int, stars: int = 0, *, name: str | None = None, quality: float = 0.0,
                    speed: int = 0, uni: str | None = None, bot: bool = False, credits: int = 777,
                    layout: dict | None = LAYOUT, snapshot: dict | None = None) -> int:
    uid = await db.fetchval("INSERT INTO users (tg_id, first_name, is_bot) VALUES ($1, $2, $3) RETURNING id",
                            tg, name or f"P{tg}", bot)
    state = {"sim": {"layout": layout, "credits": credits, "stock": {"secret": 1}}} if layout else {}
    await db.execute(
        "INSERT INTO factories (user_id, season_id, name, state, stars, quality_avg, speed_bph, university, is_bot, "
        "level, credits, snapshot) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, 3, $10, $11::jsonb)",
        uid, season_id, f"Цех {tg}", json.dumps(state), stars, quality, speed, uni, bot, credits,
        json.dumps(snapshot) if snapshot else None)
    return uid


async def table(db, me_stars: int, me_tg: int = 5000) -> int:
    """Season + 60 others (tg 1000+k, stars 10000 - 100k, k = 0..59, lower user id than `me`) + the caller 'Me'."""
    sid = await mk_season(db)
    for k in range(60):
        await mk_player(db, sid, 1000 + k, 10_000 - 100 * k)
    await mk_player(db, sid, me_tg, me_stars, name="Me", uni="МАИ")
    return sid


def board(c, tab="general", tg=5000, **params):
    return c.get("/api/season/leaderboard", params={"tab": tab, **params}, headers=dev(tg, "Me"))


# ---------------------------------------------------------------- errors

ROUTES = ["/api/season/leaderboard", "/api/factory/1", "/api/season/summary"]


@pytest.mark.parametrize("path", ROUTES)
async def test_401(make_client, path):
    c = await make_client()
    assert_error(await c.get(path), 401, "unauthorized")
    assert_error(await c.get(path, headers={"Authorization": "tma not-init-data"}), 401, "invalid_init_data")
    assert_error(await c.get(path, headers=tma(token="999:OTHER-TOKEN")), 401, "invalid_init_data")


async def test_422_invalid_tab_and_bad_params(game):
    for tab in ("nope", "", "GENERAL", "general;drop"):
        err = assert_error(await board(game, tab), 422, "invalid_tab")
        assert err["details"]["allowed"] == list(TABS)
    for bad in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"season_id": 0}, {"season_id": "x"},
                {"university": "\x00"}, {"university": "МГУ\x00"}, {"university_id": "\x00"}, {"university": "a\nb"},
                {"university": "x" * 201}):
        assert_error(await board(game, **bad), 422, "validation_error")


async def test_404_unknown_season_player_and_factory(game):
    sid = await table(game.db, 100)
    assert_error(await board(game, season_id=sid + 5), 404, "season_not_found")
    for pid in ("999999", "abc", "-1", "1.5", "99999999999999999999999", "%20",
                "9" * 19, "9" * 5000, "0" * 5000 + "1"):  # > bigint max; > int()'s 4300-digit limit
        assert_error(await game.get(f"/api/factory/{pid}", headers=dev(5000, "Me")), 404, "player_not_found")
    assert_error(await game.get("/api/season/summary", headers=dev(77)), 404, "factory_not_found")  # never bootstrapped


async def test_404_no_season_yet(game):
    assert_error(await game.get("/api/season/leaderboard", headers=dev(1)), 404, "season_not_found")


@pytest.mark.parametrize("path", ROUTES[:1] + ["/api/factory/1", "/api/season/summary"])
async def test_429_with_retry_after(make_client, clean_db, clean_redis, path):
    name = "leaderboard" if "leaderboard" in path else "default"
    c = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, rate_limit_overrides={name: 2})
    await table(clean_db, 1)
    for _ in range(2):
        assert (await c.get(path, headers=dev(5000, "Me"))).status_code in (200, 404)  # 404: no such player / factory
    resp = await c.get(path, headers=dev(5000, "Me"))
    err = assert_error(resp, 429, "rate_limited")
    assert 1 <= int(resp.headers["retry-after"]) <= 60 and err["details"]["limit"] == 2


@pytest.mark.parametrize("path", ROUTES)
async def test_500_is_the_unified_internal_error(game, monkeypatch, path):
    await table(game.db, 1)

    async def boom(*a, **k):
        raise RuntimeError("secret detail")

    for name in ("_compute", "visit", "summary"):
        monkeypatch.setattr(f"app.leaderboard.LeaderboardService.{name}", boom)
    resp = await game.get(path.replace("/1", "/3"), headers=dev(5000, "Me"))
    err = assert_error(resp, 500, "internal_error")
    assert "secret" not in json.dumps(err)


# ---------------------------------------------------------------- me block / gapTo51 against the hand-built table

# others: place k+1 has 10000 - 100k  (place 50 = 5100, place 51 = 5000, ..., place 60 = 4100); `me` joins with id > all
@pytest.mark.parametrize("stars,place,cutoff,gap", [
    (10_050, 1, 5_200, 4_950),   # 1st: place 50 is now k=48 (5200), place 51 is k=49 (5100): 10050 - 5100
    (5_150, 50, 5_150, 50),      # exactly 50th: cutoff is me, place 51 = 5100
    (5_050, 51, 5_100, 50),      # 51st: place 50 = 5100: gap to the zone = 5100 - 5050
    (5_000, 52, 5_100, 100),     # tie with k=50 (lower user id sorts first): 52nd
    (0, 61, 5_100, 5_100),       # last of 61
])
async def test_me_block_and_gap_to_51(game, stars, place, cutoff, gap):
    await table(game.db, stars)
    body = (await board(game)).json()
    assert body["tab"] == "general" and body["total"] == 61
    assert body["me"]["place"] == place and body["me"]["value"] == stars
    assert body["me"] == {"place": place, "playerId": str(body["me"]["playerId"]), "name": "Me", "factoryName": "Цех 5000",
                          "university": "МАИ", "value": stars, "isMe": True}
    assert body["inviteCutoff"] == cutoff and body["gapTo51"] == gap
    values = [r["value"] for r in body["rows"]]
    assert values == sorted(values, reverse=True) and [r["place"] for r in body["rows"]] == list(range(1, 62))
    assert [r["playerId"] for r in body["rows"] if r.get("isMe")] == [body["me"]["playerId"]]
    assert body["rows"][place - 1]["value"] == stars and body["rows"][place - 1]["isMe"] is True


async def test_rows_limit_top100_and_player_without_factory(game):
    sid = await mk_season(game.db)
    for k in range(120):
        await mk_player(game.db, sid, 1000 + k, k)
    body = (await board(game, tg=4242)).json()  # authenticated, no factory: rows, no me
    assert len(body["rows"]) == 100 and body["total"] == 120 and "me" not in body and "gapTo51" not in body
    assert body["rows"][0]["value"] == 119 and body["rows"][99]["place"] == 100
    assert body["inviteCutoff"] == 70 and "isMe" not in body["rows"][0]
    assert [r["place"] for r in (await board(game, tg=4242, limit=3)).json()["rows"]] == [1, 2, 3]
    assert len((await board(game, tg=4242, limit=100)).json()["rows"]) == 100


async def test_small_board_has_no_invite_cutoff(game):
    sid = await mk_season(game.db)
    await mk_player(game.db, sid, 1, 50)
    await mk_player(game.db, sid, 5000, 70, name="Me")
    body = (await board(game)).json()
    assert body["inviteCutoff"] == 0 and body["gapTo51"] == 70 and body["me"]["place"] == 1 and body["total"] == 2


async def test_quality_and_speed_tabs(game):
    sid = await mk_season(game.db)
    await mk_player(game.db, sid, 1, 5, quality=0.9347, speed=900)
    await mk_player(game.db, sid, 2, 9, quality=0.5, speed=1500)
    await mk_player(game.db, sid, 5000, 1, name="Me", quality=0.97, speed=1200)
    q = (await board(game, "quality")).json()
    assert [r["value"] for r in q["rows"]] == [97.0, 93.5, 50.0] and q["me"]["place"] == 1 and q["me"]["value"] == 97.0
    s = (await board(game, "speed")).json()
    assert [r["value"] for r in s["rows"]] == [1500, 1200, 900] and s["me"]["place"] == 2
    assert s["gapTo51"] == 1200 - 0  # fewer than 51 players: the cutoff is 0
    g = (await board(game)).json()
    assert [r["value"] for r in g["rows"]] == [9, 5, 1] and g["me"]["place"] == 3


async def test_uni_tab_aggregate_and_university_filter(game):
    sid = await mk_season(game.db)
    await mk_player(game.db, sid, 1, 100, uni="МАИ")
    await mk_player(game.db, sid, 2, 300, uni="МИФИ")
    await mk_player(game.db, sid, 3, 50, uni="МАИ")
    await mk_player(game.db, sid, 4, 999)  # no university: not in the aggregate
    await mk_player(game.db, sid, 5000, 10, name="Me", uni="МАИ")
    uni = (await board(game, "uni")).json()
    assert uni["universities"] == [{"name": "МИФИ", "value": 300}, {"name": "МАИ", "value": 160}]
    assert uni["tab"] == "uni" and uni["total"] == 5 and uni["me"]["place"] == 5  # rows rank players by stars
    assert "universities" not in (await board(game)).json()
    for param in ("university", "university_id"):
        f = (await board(game, **{param: "МАИ"})).json()
        assert [r["value"] for r in f["rows"]] == [100, 50, 10] and f["total"] == 3 and f["me"]["place"] == 3
    other = (await board(game, university="МИФИ")).json()
    assert other["total"] == 1 and "me" not in other and "gapTo51" not in other  # the caller studies elsewhere
    assert (await board(game, university="Нет такого")).json()["rows"] == []


async def test_season_id_selects_the_board_and_default_is_the_open_season(game):
    old = await mk_season(game.db, closed=True)
    new = await mk_season(game.db)
    await mk_player(game.db, old, 1, 111)
    await mk_player(game.db, new, 2, 222)
    await mk_player(game.db, new, 5000, 5, name="Me")
    assert [r["value"] for r in (await board(game)).json()["rows"]] == [222, 5]
    past = (await board(game, season_id=old)).json()
    assert [r["value"] for r in past["rows"]] == [111] and past["seasonId"] == old and "me" not in past  # Me plays in `new`


# ---------------------------------------------------------------- cache

async def test_cache_hit_within_ttl_me_is_fresh(game):
    sid = await mk_season(game.db)
    await mk_player(game.db, sid, 1, 100)
    me_id = await mk_player(game.db, sid, 5000, 50, name="Me")
    first = (await board(game)).json()
    keys = await game.redis.keys("lb:*")
    assert keys == [f"lb:{sid}:general::1"] and 0 < await game.redis.ttl(keys[0]) <= 30
    await game.db.execute("UPDATE factories SET stars = 999 WHERE user_id = (SELECT id FROM users WHERE tg_id = 1)")
    await game.db.execute("UPDATE factories SET stars = 500 WHERE user_id = $1", me_id)
    await mk_player(game.db, sid, 3, 700)
    second = (await board(game)).json()
    assert [r["value"] for r in second["rows"]] == [r["value"] for r in first["rows"]] == [100, 50]  # cached top
    assert second["me"]["value"] == 500 and second["me"]["place"] == 3  # me block: per request, never cached
    assert second["total"] == 3
    await game.redis.delete(keys[0])
    assert [r["value"] for r in (await board(game)).json()["rows"]] == [999, 700, 500]


async def test_cache_is_per_tab_university_and_shared_between_users(game):
    sid = await mk_season(game.db)
    await mk_player(game.db, sid, 1, 100, uni="МАИ")
    await mk_player(game.db, sid, 5000, 50, name="Me")
    await mk_player(game.db, sid, 6000, 70, name="Other")
    await board(game)
    await board(game, "speed")
    await board(game, university="МАИ")
    assert sorted(await game.redis.keys("lb:*")) == sorted(
        [f"lb:{sid}:general::1", f"lb:{sid}:speed::1", f"lb:{sid}:general:МАИ:1"])
    other = (await game.get("/api/season/leaderboard", headers=dev(6000, "Other"))).json()  # served from the same entry
    assert [r.get("isMe") for r in other["rows"]] == [None, True, None] and other["me"]["name"] == "Other"
    mine = (await board(game)).json()
    assert [r.get("isMe") for r in mine["rows"]] == [None, None, True]  # isMe is per request, not cached
    assert b"isMe" not in (await game.redis.get(f"lb:{sid}:general::1")).encode()


async def test_redis_down_fails_open(game):
    sid = await mk_season(game.db)
    await mk_player(game.db, sid, 1, 100)
    await mk_player(game.db, sid, 5000, 50, name="Me")
    game.app.state.redis = DeadRedis()
    body = (await board(game)).json()
    assert [r["value"] for r in body["rows"]] == [100, 50] and body["me"]["place"] == 2
    await game.db.execute("UPDATE factories SET stars = 500 WHERE stars = 100")
    assert (await board(game)).json()["rows"][0]["value"] == 500  # no cache: always fresh


# ---------------------------------------------------------------- bots

async def test_bots_are_in_the_board_and_excluded_when_disabled(make_client, clean_db, clean_redis):
    sid = await mk_season(clean_db)
    await mk_player(clean_db, sid, BOT_TG_BASE, 900, bot=True, uni="МФТИ", name="kvasboss")
    await mk_player(clean_db, sid, 1, 100, uni="МАИ")
    await mk_player(clean_db, sid, 5000, 50, name="Me")
    on = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, rate_limit_enabled=False)
    body = (await board(on, "uni")).json()
    assert [r["name"] for r in body["rows"]] == ["kvasboss", "P1", "Me"] and body["me"]["place"] == 3
    assert {u["name"] for u in body["universities"]} == {"МФТИ", "МАИ"} and body["total"] == 3
    bot_id = body["rows"][0]["playerId"]
    assert (await on.get(f"/api/factory/{bot_id}", headers=dev(5000, "Me"))).status_code == 200
    await clean_redis.flushdb()
    off = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, rate_limit_enabled=False,
                            bots_enabled=False)
    body = (await board(off, "uni")).json()
    assert [r["name"] for r in body["rows"]] == ["P1", "Me"] and body["me"]["place"] == 2 and body["total"] == 2
    assert body["universities"] == [{"name": "МАИ", "value": 100}] and body["inviteCutoff"] == 0
    assert [r["name"] for r in (await board(off, "speed")).json()["rows"]] == ["P1", "Me"]
    assert_error(await off.get(f"/api/factory/{bot_id}", headers=dev(5000, "Me")), 404, "player_not_found")


async def test_seed_bots_is_idempotent_deterministic_and_grows(game):
    db = game.db
    sm = game.app.state.sessionmaker
    async with sm() as session, session.begin():
        first = await seed(session, 40, progress=0.3)
    assert first["created"] == 40 and first["bots"] == 40
    ids = await db.fetch("SELECT id, tg_id FROM users WHERE is_bot ORDER BY tg_id DESC")
    assert len(ids) == 40 and ids[0]["tg_id"] == BOT_TG_BASE and all(r["tg_id"] < 0 for r in ids)
    stars_30 = {r["user_id"]: r["stars"] for r in await db.fetch("SELECT user_id, stars FROM factories WHERE is_bot")}
    assert len(stars_30) == 40 and sum(stars_30.values()) > 0
    row = await db.fetchrow("SELECT f.state, f.university, f.level, f.season_id FROM factories f JOIN users u ON u.id = f.user_id "
                            "WHERE u.tg_id = $1", BOT_TG_BASE)
    state = json.loads(row["state"])
    assert state["bot"] == gen_bot(0)["curve"] and state["sim"]["layout"]["buildings"] and row["university"]
    assert (await db.fetchval("SELECT count(DISTINCT university) FROM factories WHERE is_bot")) > 3
    # rerun: nothing new, never lowers a value in the same season; a larger count adds only the missing bots
    async with sm() as session, session.begin():
        again = await seed(session, 40, progress=0.1)
    assert again["created"] == 0 and await db.fetchval("SELECT count(*) FROM users WHERE is_bot") == 40
    assert {r["user_id"]: r["stars"] for r in await db.fetch("SELECT user_id, stars FROM factories WHERE is_bot")} == stars_30
    async with sm() as session, session.begin():
        more = await seed(session, 45, progress=1.0)
    assert more["created"] == 5 and more["bots"] == 45
    assert gen_bot(7) == gen_bot(7) and gen_bot(7)["nick"] != "" and gen_bot(7) != gen_bot(8)
    top = await db.fetchval("SELECT max(stars) FROM factories WHERE is_bot")
    assert top == max(round(gen_bot(i)["curve"]["final"]) for i in range(45))  # p = 1: final stars
    # the leaderboard shows them (Me is the last of 46) with real-looking values
    await mk_player(db, more["season_id"], 5000, 0, name="Me")
    body = (await board(game)).json()
    assert body["total"] == 46 and body["me"]["place"] == 46 and len(body["rows"]) == 46
    assert all(r["university"] for r in body["rows"][:45])
    q = (await board(game, "quality")).json()["rows"]
    assert all(0 < r["value"] <= 100 for r in q[:45]) and (await board(game, "speed")).json()["rows"][0]["value"] > 500


def test_bot_curves():
    c = gen_bot(3)["curve"]
    assert bot_values(c, 0)["stars"] == 0 and bot_values(c, 1)["stars"] == c["final"]
    assert bot_values(c, 0.3)["stars"] < bot_values(c, 0.6)["stars"] < bot_values(c, 1.5)["stars"] == c["final"]
    assert 0 < bot_values(c, 0.5)["quality_avg"] <= 1 and bot_values(c, 0.5)["speed_bph"] > 0


async def test_grow_bots_moves_a_bot_into_a_new_season(game):
    sm = game.app.state.sessionmaker
    async with sm() as session, session.begin():
        s1 = await seed(session, 3, progress=1.0)
    new = await mk_season(game.db)
    async with sm() as session, session.begin():
        await grow_bots(session, new, 0.0)
    rows = await game.db.fetch("SELECT season_id, stars FROM factories WHERE is_bot")
    assert {r["season_id"] for r in rows} == {new} != {s1["season_id"]} and all(r["stars"] == 0 for r in rows)


# ---------------------------------------------------------------- GET /api/factory/{playerId}

async def test_visit_returns_the_public_cut_only(game):
    sid = await mk_season(game.db)
    uid = await mk_player(game.db, sid, 1, 4321, name="Alice", uni="МАИ", credits=987_654,
                          snapshot={"factory": {"credits": 987_654}})
    resp = await game.get(f"/api/factory/{uid}", headers=dev(5000, "Me"))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"player", "factory"}
    assert body["player"] == {"id": str(uid), "name": "Alice", "university": "МАИ"}
    assert set(body["factory"]) == {"name", "level", "stars", "layout", "updatedAt"}
    assert body["factory"]["layout"] == LAYOUT and body["factory"]["stars"] == 4321 and body["factory"]["level"] == 3
    assert body["factory"]["name"] == "Цех 1" and isinstance(body["factory"]["updatedAt"], int)
    assert body["factory"]["updatedAt"] > 1_600_000_000_000
    raw = resp.text
    for private in ("987654", "credits", "loan", "stock", "secret", "history", "xp", "shipped", "orders"):
        assert private not in raw, private
    no_uni = await mk_player(game.db, sid, 2, 1, layout=LAYOUT)
    assert "university" not in (await game.get(f"/api/factory/{no_uni}", headers=dev(5000, "Me"))).json()["player"]
    # a factory without any layout is not a visitable one
    broken = await mk_player(game.db, sid, 3, 1, layout=None)
    assert_error(await game.get(f"/api/factory/{broken}", headers=dev(5000, "Me")), 404, "player_not_found")


async def test_visit_falls_back_to_the_snapshot_layout_and_me_route_wins(game, fake_sim):
    sid = await mk_season(game.db)
    uid = await mk_player(game.db, sid, 1, 10, layout=None, snapshot={"factory": {"layout": LAYOUT}})
    assert (await game.get(f"/api/factory/{uid}", headers=dev(5000, "Me"))).json()["factory"]["layout"] == LAYOUT
    boot = await bootstrap(game, 9)
    assert (await me(game, 9)).json()["snapshot"]["factory"]["name"] == boot["snapshot"]["factory"]["name"]  # /me, not an id


async def test_visit_a_real_player_after_bootstrap_shows_their_state_layout(game, fake_sim):
    await bootstrap(game, 1, "Boss")
    res = await command(game, 0, key(1), shift(), 1)
    assert res.status_code == 200
    uid = (await factory_row(game, 1))["user_id"]
    v = (await game.get(f"/api/factory/{uid}", headers=dev(2))).json()
    assert v["player"]["name"] == await game.db.fetchval("SELECT first_name FROM users WHERE tg_id = 1") and v["factory"]["layout"]["buildings"][0]["id"] == "b1"
    assert "credits" not in v["factory"] and "snapshot" not in v


# ---------------------------------------------------------------- GET /api/season/summary

async def test_summary_shape_and_values(game):
    sid = await mk_season(game.db)
    for k in range(60):
        await mk_player(game.db, sid, 1000 + k, 10_000 - 100 * k)
    skills = {"byProfession": {"lab": {"played": 4, "wins": 3, "avgScore": 0.7},
                               "operator": {"played": 1, "wins": 0, "avgScore": 0.2}}}
    snap = {"factory": {"produced": 12345.6, "collection": {"kvas_15": 1, "kvas_05": 2},
                        "layout": {"buildings": [{"kind": "packer"}, {"kind": "filler"}, {"kind": "packer"}]}},
            "skills": skills}
    await mk_player(game.db, sid, 5000, 5_050, name="Me", quality=0.9, credits=4242, snapshot=snap)
    resp = await game.get("/api/season/summary", headers=dev(5000, "Me"))
    assert resp.status_code == 200
    s = resp.json()
    assert s == {"place": 51, "total": 61, "inInviteZone": False, "bottles": 12345, "quality": pytest.approx(0.9),
                 "lines": 2, "skus": 2, "bestProfession": "lab",
                 "bestProfessionReason": "лучшие смены — «НАЙДИ БРАК»: 3 из 4, средний счёт 70",
                 "seasonId": sid, "credits": 4242, "stars": 5050, "level": 3, "quizResult": None, "leadStatus": None}
    await game.db.execute("UPDATE factories SET stars = 9999 WHERE user_id = (SELECT id FROM users WHERE tg_id = 5000)")
    top = (await game.get("/api/season/summary", headers=dev(5000, "Me"))).json()
    assert top["place"] == 2 and top["inInviteZone"] is True


async def test_summary_of_a_fresh_factory_and_best_profession_rules(game, fake_sim):
    await bootstrap(game, 1)
    s = (await game.get("/api/season/summary", headers=dev(1))).json()
    assert s["place"] == 1 and s["total"] == 1 and s["bottles"] == 0 and s["lines"] == 0 and s["skus"] == 0
    assert s["bestProfession"] == "operator" and "ещё не выходил" in s["bestProfessionReason"]
    assert s["credits"] > 0 and s["quizResult"] is None and s["leadStatus"] is None
    assert best_profession(None)[0] == "operator" and best_profession({"byProfession": {}})[0] == "operator"
    two = {"byProfession": {"a": {"played": 10, "wins": 1, "avgScore": 0.1}, "kipia": {"played": 2, "wins": 2, "avgScore": 1}}}
    assert best_profession(two)[0] == "kipia" and "СОЕДИНИ" in best_profession(two)[1]


# ---------------------------------------------------------------- snapshot.rank + denormalised leaderboard columns

async def test_snapshot_rank_is_the_real_leaderboard_place(game, fake_sim):
    sid = await mk_season(game.db)
    for k in range(5):
        await mk_player(game.db, sid, 1000 + k, 100 * (k + 1))  # 100..500
    boot = await bootstrap(game, 1)  # the fake sim player starts with 0 stars: last of 6
    assert boot["snapshot"]["rank"] == {"place": 6, "total": 6}
    assert (await board(game, tg=1)).json()["me"]["place"] == 6
    stored = (await factory_row(game, 1))["snapshot"]
    assert stored["rank"] == {"place": 6, "total": 6}  # the stored (degraded-mode) copy carries it as well
    await game.db.execute("UPDATE factories SET stars = 350 WHERE user_id = (SELECT id FROM users WHERE tg_id = 1)")
    for resp in (await me(game, 1), await advance(game, None, 1)):  # fake sim resets stars to its own 0 -> back to last
        assert resp.json()["snapshot"]["rank"] == {"place": 6, "total": 6}
    ok = await command(game, boot["version"], key(1), {"type": "renameFactory", "name": "Zed"})
    assert ok.json()["snapshot"]["rank"] == {"place": 6, "total": 6}
    replay = await command(game, boot["version"], key(1), {"type": "renameFactory", "name": "Zed"})
    assert replay.json()["replayed"] is True and replay.json()["snapshot"]["rank"] == {"place": 6, "total": 6}
    await game.db.execute("INSERT INTO users (tg_id, first_name) VALUES (2000, 'N')")  # a 7th player appears
    uid = await game.db.fetchval("SELECT id FROM users WHERE tg_id = 2000")
    await game.db.execute("INSERT INTO factories (user_id, season_id, name, state, stars) VALUES ($1, $2, 'x', '{}', 5)", uid, sid)
    assert (await me(game, 1)).json()["snapshot"]["rank"] == {"place": 7, "total": 7}  # 5 stars > 0: behind the newcomer
    rejected = await command(game, boot["version"] + 1, key(2), shift(durationMs=100))  # refused by the API (too short)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["details"]["snapshot"]["rank"]["total"] == 7


async def test_set_university_syncs_user_and_factory_columns(game, fake_sim):
    boot = await bootstrap(game, 1, "Uni")
    res = await command(game, boot["version"], key(1), {"type": "setUniversity", "university": "МФТИ"})
    assert res.status_code == 200, res.text
    assert (await factory_row(game, 1))["university"] == "МФТИ"
    assert await game.db.fetchval("SELECT university FROM users WHERE tg_id = 1") == "МФТИ"
    me_row = (await game.get("/api/season/leaderboard", headers=dev(1, "Uni"))).json()["me"]
    assert me_row["university"] == "МФТИ"
    again = await command(game, boot["version"] + 1, key(2), {"type": "setUniversity", "university": "МАИ"})
    assert again.status_code == 200 and (await factory_row(game, 1))["university"] == "МАИ"


def test_denormalise_quality_university_and_speed_column_sources():
    fresh = {"factory": {"quality": 1, "produced": 0, "credits": 5}, "player": {"university": "  МАИ "}}
    d = denormalise(fresh)
    assert d["quality_avg"] == 0.0 and d["university"] == "МАИ"  # nothing produced: not "100 % quality"
    assert denormalise({"factory": {"quality": 0.93, "produced": 10}})["quality_avg"] == pytest.approx(0.93)
    assert denormalise({"factory": {"quality": 0.5}})["quality_avg"] == 0.5  # produced unknown: keep the sim's number
    assert "university" not in denormalise({"factory": {}, "player": {"university": ""}})


def test_measure_speed():
    def snap(produced, t, **extra):
        return {"factory": {"produced": produced, "lastTick": t, **extra}}

    state: dict = {}
    assert measure_speed(snap(100, 1_000_000), state) is None and state["meta"]["speed"] == {"t": 1_000_000, "p": 100}
    # polled every few seconds: the baseline stays, nothing is reported yet
    assert measure_speed(snap(110, 1_000_000 + 5_000), state) is None
    assert state["meta"]["speed"] == {"t": 1_000_000, "p": 100}
    assert measure_speed(snap(100 + 600, 1_000_000 + H // 2), state) == 1200  # 600 bottles in half an hour
    assert state["meta"]["speed"] == {"t": 1_000_000 + H // 2, "p": 700}
    assert measure_speed(snap(700, 1_000_000 + H // 2 + SPEED_WINDOW_MS), state) == 0  # idle for the window
    # offline longer than the 8 h cap: the window is capped, not the production
    big = {"meta": {"speed": {"t": 0, "p": 0}}}
    assert measure_speed(snap(8_000, 20 * H), big) == 1000
    # a new season resets `produced`: re-baseline, keep the stored speed
    reset = {"meta": {"speed": {"t": 5 * H, "p": 5000}}}
    assert measure_speed(snap(3, 6 * H), reset) is None and reset["meta"]["speed"] == {"t": 6 * H, "p": 3}
    # an explicit rate from the sim wins, and snapshots without the fields leave everything alone
    assert measure_speed(snap(1, 2, bottlesPerHour=1234.6), {}) == 1235
    plain: dict = {}
    assert measure_speed({"factory": {"credits": 1}}, plain) is None and plain == {}
