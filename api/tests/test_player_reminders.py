"""S6 player side: ageConfirm -> users.age_confirmed, migration 0007 (reminder consent columns, reminders_sent, backfill),
per-minute request counters of the bot's 5xx alert."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.metrics import ERR_PREFIX, REQ_PREFIX, TTL_S, counted, minute_stamp, record_request
from tests.conftest import pg_dsn
from tests.test_db import alembic as alembic_cli
from tests.game_helpers import bootstrap, command, key
from tests.helpers import DeadRedis, TEST_DB_URL, dev


# ---------------------------------------------------------------- ageConfirm -> users.age_confirmed

async def user_row(game, tg: int = 1):
    return await game.db.fetchrow("SELECT age_confirmed, age_confirmed_at FROM users WHERE tg_id = $1", tg)


async def test_age_confirm_sets_user_flag_in_the_same_transaction(game):
    boot = await bootstrap(game, 1, "Ann")
    before = await user_row(game)
    assert before["age_confirmed"] is False and before["age_confirmed_at"] is None
    assert boot["snapshot"]["player"].get("ageConfirmed") in (None, False)

    res = await command(game, boot["version"], key(1), {"type": "ageConfirm"})
    assert res.status_code == 200, res.text
    assert res.json()["snapshot"]["player"]["ageConfirmed"] is True
    after = await user_row(game)
    assert after["age_confirmed"] is True and after["age_confirmed_at"] is not None

    # later commands / a repeated ageConfirm keep the first moment
    again = await command(game, boot["version"] + 1, key(2), {"type": "ageConfirm"})
    assert again.status_code == 200
    await command(game, boot["version"] + 2, key(3), {"type": "renameFactory", "name": "Zavod"})
    assert (await user_row(game))["age_confirmed_at"] == after["age_confirmed_at"]


async def test_other_commands_and_replays_do_not_touch_the_flag(game):
    boot = await bootstrap(game, 2, "Bob")
    assert (await command(game, boot["version"], key(1), {"type": "renameFactory", "name": "X"}, tg=2)).status_code == 200
    assert (await user_row(game, 2))["age_confirmed"] is False
    # a replay of the same key does not run the sim again and changes nothing
    ok = await command(game, boot["version"] + 1, key(2), {"type": "ageConfirm"}, tg=2)
    replay = await command(game, boot["version"] + 1, key(2), {"type": "ageConfirm"}, tg=2)
    assert ok.status_code == replay.status_code == 200 and replay.json()["replayed"] is True
    assert (await user_row(game, 2))["age_confirmed"] is True


async def test_age_confirm_does_not_leak_to_other_users(game):
    a, b = await bootstrap(game, 10, "A"), await bootstrap(game, 11, "B")
    await command(game, a["version"], key(1), {"type": "ageConfirm"}, tg=10)
    assert (await user_row(game, 10))["age_confirmed"] is True
    assert (await user_row(game, 11))["age_confirmed"] is False
    assert b["version"] == 0


# ---------------------------------------------------------------- migration 0007

async def test_migration_0007_columns_defaults_and_cascade(clean_db):
    await clean_db.execute("INSERT INTO users (tg_id) VALUES (501)")
    row = await clean_db.fetchrow(
        "SELECT bot_started_at, notify_opt_in, notify_opt_in_at, notify_opt_out_at FROM users WHERE tg_id = 501")
    assert dict(row) == {"bot_started_at": None, "notify_opt_in": False, "notify_opt_in_at": None,
                         "notify_opt_out_at": None}
    uid = await clean_db.fetchval("SELECT id FROM users WHERE tg_id = 501")
    await clean_db.execute("INSERT INTO reminders_sent (user_id, order_id, kind) VALUES ($1, 'o1', 'eta')", uid)
    with pytest.raises(asyncpg.UniqueViolationError):  # pk (user_id, order_id, kind)
        await clean_db.execute("INSERT INTO reminders_sent (user_id, order_id, kind) VALUES ($1, 'o1', 'eta')", uid)
    await clean_db.execute("INSERT INTO reminders_sent (user_id, order_id, kind) VALUES ($1, 'o1', 'deadline')", uid)
    assert await clean_db.fetchval("SELECT sent_at IS NOT NULL FROM reminders_sent WHERE kind = 'eta'")
    await clean_db.execute("DELETE FROM users WHERE id = $1", uid)
    assert await clean_db.fetchval("SELECT count(*) FROM reminders_sent") == 0  # ON DELETE CASCADE


async def test_migration_0007_backfills_age_confirmed_from_snapshots(pg_url):
    down = alembic_cli("downgrade", "0006")
    assert down.returncode == 0, down.stderr
    conn = await asyncpg.connect(pg_dsn(TEST_DB_URL))
    try:
        await conn.execute("TRUNCATE factories, seasons, users RESTART IDENTITY CASCADE")
        sid = await conn.fetchval("INSERT INTO seasons (starts_at, ends_at, days) "
                                  "VALUES (now(), now() + interval '7 days', 7) RETURNING id")
        for tg, snap in ((601, {"player": {"ageConfirmed": True}}), (602, {"player": {"ageConfirmed": False}}),
                         (603, {"player": {}}), (604, None)):
            uid = await conn.fetchval("INSERT INTO users (tg_id) VALUES ($1) RETURNING id", tg)
            await conn.execute("INSERT INTO factories (user_id, season_id, name, state, snapshot) VALUES ($1, $2, 'f', '{}', $3)",
                               uid, sid, json.dumps(snap) if snap is not None else None)
        await conn.execute("INSERT INTO users (tg_id) VALUES (605)")  # no factory at all
        up = alembic_cli("upgrade", "head")
        assert up.returncode == 0, up.stderr
        rows = {r["tg_id"]: r for r in await conn.fetch("SELECT tg_id, age_confirmed, age_confirmed_at FROM users")}
        assert rows[601]["age_confirmed"] is True and rows[601]["age_confirmed_at"] is not None
        assert [rows[t]["age_confirmed"] for t in (602, 603, 604, 605)] == [False] * 4
        assert [rows[t]["age_confirmed_at"] for t in (602, 603, 604, 605)] == [None] * 4
    finally:
        await conn.execute("TRUNCATE factories, seasons, users RESTART IDENTITY CASCADE")
        await conn.close()


# ---------------------------------------------------------------- request counters (bot 5xx alert)

async def metric_sums(redis) -> tuple[int, int]:
    req = sum([int(await redis.get(k) or 0) async for k in redis.scan_iter(REQ_PREFIX + "*")])
    err = sum([int(await redis.get(k) or 0) async for k in redis.scan_iter(ERR_PREFIX + "*")])
    return req, err


async def test_middleware_counts_requests_and_5xx_per_minute(live):
    assert await metric_sums(live.redis) == (0, 0)
    assert (await live.get("/t/small")).status_code == 200
    assert (await live.get("/api/bootstrap")).status_code == 401  # 4xx: a request, not an error
    assert (await live.get("/t/boom")).status_code == 500
    assert (await live.get("/t/apperror/sim_unavailable")).status_code == 503
    assert await metric_sums(live.redis) == (4, 2)

    keys = [k async for k in live.redis.scan_iter("metrics:*")]
    assert keys and all(k.rsplit(":", 1)[1].isdigit() and len(k.rsplit(":", 1)[1]) == 12 for k in keys)
    for k in keys:
        assert 0 < await live.redis.ttl(k) <= TTL_S


async def test_probes_and_internal_calls_are_not_counted(live):
    for path in ("/health", "/ready", "/internal/seasons/rotate", "/internal/t/sleep/0"):
        await live.get(path)
        await live.post(path)
    assert await metric_sums(live.redis) == (0, 0)


async def test_counters_fail_open_without_redis(make_client):
    c = await make_client()  # DeadRedis: every operation raises
    assert isinstance(c.app.state.redis, DeadRedis)
    assert (await c.get("/t/small")).status_code == 200
    assert (await c.get("/t/boom")).status_code == 500


async def test_record_request_rules(clean_redis):
    at = datetime(2026, 10, 24, 12, 34, 56, tzinfo=timezone.utc)
    assert minute_stamp(at) == "202610241234"
    assert minute_stamp(datetime(2026, 10, 24, 15, 34, tzinfo=timezone(timedelta(hours=3)))) == "202610241234"  # UTC
    r = clean_redis
    assert await record_request(r, "/api/command", 200, at) is True
    assert await record_request(r, "/api/command", 499, at) is True
    assert await record_request(r, "/api/command", 500, at) is True
    assert await record_request(r, "/api/command", 503, at) is True
    assert await record_request(r, "/api/command", None, at) is False  # response never started
    assert await record_request(None, "/api/command", 500, at) is False
    assert await record_request(r, "/health", 503, at) is False
    assert await record_request(r, "/internal/top/advance", 500, at) is False
    assert (await r.get(REQ_PREFIX + "202610241234"), await r.get(ERR_PREFIX + "202610241234")) == ("4", "2")
    assert counted("/api/bootstrap") and not counted("/ready") and not counted("/internal/x")
    assert await record_request(DeadRedis(), "/api/command", 500, at) is False  # swallowed
