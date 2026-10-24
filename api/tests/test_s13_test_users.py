"""S13: test users (users.is_test) are marked by dev auth and kept out of the board, ranks, season results and the top-N
advance; purge script; migration 0009. Real Postgres + Redis (skipped when absent), in-process fake sim."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import asyncpg
import pytest

from app.purge_test_users import purge
from app.testusers import TEST_TG_ID_MIN, is_test_identity
from tests.engage_helpers import lead_body
from tests.game_helpers import bootstrap
from tests.helpers import API_DIR, TEST_DB_URL, TEST_REDIS_URL, dev, tma
from tests.test_seasons import TOP, post, rotate, seed_bots, set_season_end, set_stars, srv, uid_of  # noqa: F401

BIG = TEST_TG_ID_MIN
PG_DSN = TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://")


def flags(db, tg: int):
    return db.fetchrow("SELECT u.is_test AS u_test, f.is_test AS f_test FROM users u "
                       "LEFT JOIN factories f ON f.user_id = u.id WHERE u.tg_id = $1", tg)


# ---------------------------------------------------------------- the rule

@pytest.mark.parametrize("tg, name, expected", [
    (1001, "Ivan", False), (1001, None, False), (1001, "", False), (1001, "e2e", True), (1001, "E2E", True),
    (1001, "  e2e-smoke", True), (1001, "my e2e", False), (1001, "s7", False),
    (BIG - 1, "x", False), (BIG, "x", True), (2**63 - 1, None, True),
])
def test_rule(tg, name, expected):
    assert is_test_identity(tg, name) is expected


async def test_dev_auth_marks_test_users(srv):
    for tg, name in ((1, "Ivan"), (2, "e2e"), (3, "E2E-Run"), (4, None), (BIG, "Bob"), (BIG - 1, "Bob")):
        await bootstrap(srv, tg, name)
    got = {tg: tuple((await flags(srv.db, tg)).values()) for tg in (1, 2, 3, 4, BIG, BIG - 1)}
    assert got == {1: (False, False), 2: (True, True), 3: (True, True), 4: (False, False), BIG: (True, True),
                   BIG - 1: (False, False)}
    r = await srv.get("/api/bootstrap", headers={"X-Dev-User": "77:e2e"})  # the web client's header takes ':name' too
    assert r.status_code == 200 and tuple((await flags(srv.db, 77)).values()) == (True, True)
    assert (await srv.get("/api/bootstrap", headers={"X-Dev-User": "78"})).status_code == 200
    assert tuple((await flags(srv.db, 78)).values()) == (False, False)  # the client sends no name: Dev78 is a normal player


async def test_flag_is_sticky_and_applies_to_an_existing_player(srv):
    await bootstrap(srv, 5, "Ivan")
    assert tuple((await flags(srv.db, 5)).values()) == (False, False)
    assert (await srv.get("/api/factory/me", headers=dev(5, "e2e"))).status_code == 200
    assert tuple((await flags(srv.db, 5)).values()) == (True, True)  # user AND factory
    assert (await srv.get("/api/factory/me", headers=dev(5, "Ivan"))).status_code == 200
    assert tuple((await flags(srv.db, 5)).values()) == (True, True)  # never unflagged by a later name


async def test_telegram_users_are_never_test_users(srv):
    r = await srv.get("/api/bootstrap", headers=tma({"id": BIG + 5, "first_name": "e2e"}))
    assert r.status_code == 200, r.text
    assert tuple((await flags(srv.db, BIG + 5)).values()) == (False, False)


# ---------------------------------------------------------------- board, ranks, results

async def build_table(srv):
    """Real players 1 (300 stars), 2 (200), 3 (100); test players 10 'e2e' (999) and BIG (500). All at one university."""
    for tg, name, stars in ((1, "A", 300), (2, "B", 200), (3, "C", 100), (10, "e2e", 999), (BIG, "Z", 500)):
        await bootstrap(srv, tg, name)
        await set_stars(srv.db, tg, stars)
    await srv.db.execute("UPDATE factories SET university = 'МАИ', quality_avg = stars / 1000.0, speed_bph = stars")
    await srv.redis.flushdb()


@pytest.mark.parametrize("tab", ["general", "quality", "speed", "uni"])
async def test_leaderboard_never_shows_test_users(srv, tab):
    await build_table(srv)
    r = await srv.get("/api/season/leaderboard", params={"tab": tab, "limit": 50}, headers=dev(2, "B"))
    body = r.json()
    ids = [x["playerId"] for x in body["rows"]]
    assert ids == [str(await uid_of(srv.db, tg)) for tg in (1, 2, 3)], body
    assert body["total"] == 3 and body["me"]["place"] == 2
    if tab == "uni":
        assert body["universities"] == [{"name": "МАИ", "value": 600}]  # 300 + 200 + 100, not 1999 + ...


async def test_a_test_player_sees_a_rank_but_is_not_listed(srv):
    await build_table(srv)
    body = (await srv.get("/api/season/leaderboard", params={"limit": 50}, headers=dev(10, "e2e"))).json()
    assert len(body["rows"]) == 3 and all(not x.get("isMe") for x in body["rows"])
    assert body["me"]["place"] == 1  # ahead of every real player, yet not in the rows
    summary = (await srv.get("/api/season/summary", headers=dev(2, "B"))).json()
    assert summary["place"] == 2 and summary["total"] == 3


async def test_bootstrap_rank_ignores_test_players(srv):
    await build_table(srv)
    snap = (await srv.get("/api/bootstrap", headers=dev(3, "C"))).json()["snapshot"]
    assert snap["rank"] == {"place": 3, "total": 3}


async def test_season_results_skip_test_users(srv):
    await build_table(srv)
    await set_season_end(srv.db, 1, "10 minutes")
    assert (await rotate(srv))["closed"] == [1]
    rows = await srv.db.fetch("SELECT user_id, place FROM season_results ORDER BY place")
    assert [(r["user_id"], r["place"]) for r in rows] == [(await uid_of(srv.db, tg), i) for i, tg in enumerate((1, 2, 3), 1)]


async def test_top_advance_skips_test_users(srv):
    await build_table(srv)
    srv.sim.calls.clear()
    out = (await post(srv, TOP)).json()
    assert out["advanced"] == 3 and out["failed"] == 0


# ---------------------------------------------------------------- purge

async def make_test_data(srv):
    await bootstrap(srv, 1, "Ivan")
    await seed_bots(srv, 2)
    for tg in (2, BIG):
        await bootstrap(srv, tg, "e2e")
        assert (await srv.post("/api/events", json={"events": [{"name": "app_open"}, {"name": "game_start"}]},
                               headers=dev(tg, "e2e"))).json()["accepted"] == 2
        r = await srv.post("/api/leads", json=lead_body(), headers=dev(tg, "e2e"))
        assert r.status_code in (200, 201), r.text
    assert (await srv.post("/api/events", json={"events": [{"name": "app_open"}]}, headers=dev(1, "Ivan"))).status_code == 200


async def counts(db) -> dict[str, int]:
    return {t: await db.fetchval(f"SELECT count(*) FROM {t}") for t in ("users", "factories", "leads", "events", "outbox")}


async def test_purge_dry_run_counts_and_changes_nothing(srv):
    await make_test_data(srv)
    before = await counts(srv.db)
    async with srv.app.state.sessionmaker() as session, session.begin():
        out = await purge(session, dry_run=True)
    assert out == {"users": 2, "factories": 2, "leads": 2, "events": 4, "outbox": 2}
    assert await counts(srv.db) == before


async def test_purge_removes_test_users_and_everything_of_theirs(srv):
    await make_test_data(srv)
    async with srv.app.state.sessionmaker() as session, session.begin():
        out = await purge(session)
    assert out == {"users": 2, "factories": 2, "leads": 2, "events": 4, "outbox": 2}
    # left: the real player and the 2 demo bots, with the real player's one event
    assert await srv.db.fetchval("SELECT count(*) FROM users WHERE is_test") == 0
    assert await srv.db.fetchval("SELECT count(*) FROM users") == 3
    assert await srv.db.fetchval("SELECT count(*) FROM users WHERE is_bot") == 2
    assert await srv.db.fetchval("SELECT count(*) FROM factories") == 3
    assert await srv.db.fetchval("SELECT count(*) FROM events") == 1
    assert await srv.db.fetchval("SELECT count(*) FROM events WHERE user_id IS NULL") == 0
    assert await srv.db.fetchval("SELECT count(*) FROM leads") == 0
    assert await srv.db.fetchval("SELECT count(*) FROM outbox") == 0
    async with srv.app.state.sessionmaker() as session, session.begin():
        assert (await purge(session))["users"] == 0  # idempotent


def test_purge_cli(pg_url, redis_url):
    async def seed() -> None:
        conn = await asyncpg.connect(PG_DSN)
        try:
            await conn.execute("TRUNCATE users CASCADE")
            await conn.execute("INSERT INTO users (tg_id, first_name, is_test) VALUES (1, 'Ivan', false), (2, 'e2e', true)")
        finally:
            await conn.close()

    async def left() -> list[int]:
        conn = await asyncpg.connect(PG_DSN)
        try:
            return sorted(r["tg_id"] for r in await conn.fetch("SELECT tg_id FROM users"))
        finally:
            await conn.close()

    asyncio.run(seed())
    env = {**os.environ, "DATABASE_URL": TEST_DB_URL, "REDIS_URL": TEST_REDIS_URL}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "app.purge_test_users", *args], cwd=API_DIR, env=env,
                              capture_output=True, text=True)

    dry = run("--dry-run")
    assert dry.returncode == 0, dry.stderr
    out = json.loads(dry.stdout)
    assert out["users"] == 1 and out["dry_run"] is True and asyncio.run(left()) == [1, 2]
    real = run()
    assert real.returncode == 0, real.stderr
    out = json.loads(real.stdout)
    assert out["users"] == 1 and out["dry_run"] is False and asyncio.run(left()) == [1]


# ---------------------------------------------------------------- migration 0009

def alembic(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API_DIR, capture_output=True, text=True,
                          env={**os.environ, "DATABASE_URL": TEST_DB_URL})


def test_migration_0009_backfills_and_downgrades(pg_url):
    async def run(sql: str, fetch: bool = False):
        conn = await asyncpg.connect(PG_DSN)
        try:
            return await (conn.fetch if fetch else conn.execute)(sql)
        finally:
            await conn.close()

    assert alembic("downgrade", "0008").returncode == 0
    assert asyncio.run(run("SELECT table_name FROM information_schema.columns WHERE column_name = 'is_test'", True)) == []
    asyncio.run(run("TRUNCATE users CASCADE"))
    asyncio.run(run("INSERT INTO users (tg_id, first_name, is_bot) VALUES (1, 'Ivan', false), (2, 'E2E', false), "
                    "(3, 'x', false), (1000000000000, 'y', false), (-5, 'e2e-bot', true)"))
    asyncio.run(run("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '7 days', 7)"))
    asyncio.run(run("INSERT INTO factories (user_id, season_id, name, state) "
                    "SELECT id, (SELECT min(id) FROM seasons), 'f', '{}' FROM users"))
    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    rows = asyncio.run(run("SELECT u.tg_id, u.is_test AS u, f.is_test AS f FROM users u JOIN factories f ON f.user_id = u.id "
                           "ORDER BY u.tg_id", True))
    assert [(r["tg_id"], r["u"], r["f"]) for r in rows] == [
        (-5, False, False), (1, False, False), (2, True, True), (3, False, False), (1000000000000, True, True)]
    assert alembic("downgrade", "0008").returncode == 0 and alembic("upgrade", "head").returncode == 0
