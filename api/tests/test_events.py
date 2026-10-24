"""S5: POST /api/events (sim is DOWN in every test: analytics never calls it)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import events as events_mod
from app.events import EVENT_NAMES, MAX_BATCH
from tests.engage_helpers import engage, engage_factory, js, user_id  # noqa: F401
from tests.helpers import dev
from tests.test_errors import assert_error

ANALYTICS_TS = Path(__file__).resolve().parents[2] / "vendor/frontend/web/src/lib/analytics.ts"


def now_ms(offset_s: float = 0) -> int:
    return int((datetime.now(timezone.utc).timestamp() + offset_s) * 1000)


def ev(name: str = "app_open", ts: int | None = None, **props):
    e = {"name": name, "ts": now_ms() if ts is None else ts}
    if props:
        e["props"] = props
    return e


def post(c, events, tg: int = 1):
    return c.post("/api/events", json={"events": events}, headers=dev(tg))


async def stored(c):
    return [{**dict(r), "props": js(r["props"])} for r in await c.db.fetch("SELECT * FROM events ORDER BY id")]


# ---------------------------------------------------------------- whitelist

@pytest.mark.skipif(not ANALYTICS_TS.exists(), reason="vendor/frontend is not checked out")
def test_whitelist_matches_the_frontend_event_names():
    src = ANALYTICS_TS.read_text(encoding="utf-8")
    union = re.search(r"export type EventName\s*=(.*?);", src, re.S).group(1)
    assert set(re.findall(r"'([a-z_]+)'", union)) == set(EVENT_NAMES)


# ---------------------------------------------------------------- happy paths

async def test_accepts_a_batch_and_stores_it(engage):
    await engage.get("/api/me", headers=dev(1))
    await engage.db.execute("UPDATE users SET source = 'uni_mirea'")
    batch = [ev("app_open"), ev("game_end", result="win", duration_ms=5400, difficulty=1.5), ev("lead_submit", role="lab")]
    r = await post(engage, batch)
    assert r.status_code == 200 and r.json() == {"accepted": 3}
    rows = await stored(engage)
    assert [x["name"] for x in rows] == ["app_open", "game_end", "lead_submit"]
    assert rows[1]["props"] == {"result": "win", "duration_ms": 5400, "difficulty": 1.5} and rows[0]["props"] == {}
    assert {x["source"] for x in rows} == {"uni_mirea"} and {x["user_id"] for x in rows} == {await user_id(engage)}
    assert all(abs((x["ts"] - x["received_at"]).total_seconds()) < 30 for x in rows)


async def test_every_whitelisted_name_is_accepted(engage):
    names = sorted(EVENT_NAMES)
    for i in range(0, len(names), MAX_BATCH):
        assert (await post(engage, [ev(n) for n in names[i:i + MAX_BATCH]])).status_code == 200
    assert await engage.db.fetchval("SELECT count(*) FROM events") == len(names)


async def test_exactly_50_events_and_the_empty_batch(engage):
    assert (await post(engage, [ev() for _ in range(50)])).json() == {"accepted": 50}
    assert (await post(engage, [])).json() == {"accepted": 0}
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 50


async def test_envelopes_and_timestamp_formats(engage):
    iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    r = await engage.post("/api/events", json=[ev(), {"name": "app_open", "ts": iso}, {"name": "app_open"},
                                                {"name": "app_open", "ts": "2026-01-01T10:00:00"}], headers=dev(1))
    assert r.json() == {"accepted": 4}  # bare list, ISO string, naive = UTC, missing ts = now
    rows = await stored(engage)
    assert rows[3]["ts"] == datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    assert abs((rows[2]["ts"] - datetime.now(timezone.utc)).total_seconds()) < 30


async def test_old_events_from_an_offline_device_are_fine(engage):
    assert (await post(engage, [ev(ts=now_ms(-3 * 86400))])).status_code == 200


async def test_future_within_the_skew_is_fine(engage):
    assert (await post(engage, [ev(ts=now_ms(30))])).status_code == 200


async def test_unknown_extra_fields_are_ignored(engage):
    e = {**ev(), "session": "abc", "id": 7}
    assert (await post(engage, [e])).json() == {"accepted": 1}
    assert (await stored(engage))[0]["props"] == {}


# ---------------------------------------------------------------- 422

async def test_too_many_events(engage):
    err = assert_error(await post(engage, [ev() for _ in range(51)]), 422, "too_many_events")
    assert err["details"] == {"max": 50, "got": 51}
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0


@pytest.mark.parametrize("name", ["hack", "APP_OPEN", "app_open ", "", None, 5, ["app_open"], "x" * 500])
async def test_invalid_event_name(engage, name):
    err = assert_error(await post(engage, [ev(), {"name": name, "ts": now_ms()}]), 422, "invalid_event_name")
    assert err["details"]["index"] == 1
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0  # the valid first event is not kept either


async def test_missing_name_is_invalid_event_name(engage):
    assert_error(await post(engage, [{"ts": now_ms()}]), 422, "invalid_event_name")


async def test_future_timestamp(engage):
    err = assert_error(await post(engage, [ev(), ev("game_start", ts=now_ms(120))]), 422, "future_timestamp")
    assert err["details"]["index"] == 1 and err["details"]["max_skew_s"] == 60
    iso = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    assert_error(await engage.post("/api/events", json={"events": [{"name": "app_open", "ts": iso}]}, headers=dev(1)),
                 422, "future_timestamp")
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0


async def test_future_boundary_uses_the_clock(engage, monkeypatch):
    fixed = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(events_mod, "utcnow", lambda: fixed)
    ms = int(fixed.timestamp() * 1000)
    assert (await post(engage, [ev(ts=ms + 60_000)])).status_code == 200  # exactly 60 s ahead
    assert_error(await post(engage, [ev(ts=ms + 60_001)]), 422, "future_timestamp")


@pytest.mark.parametrize("body", [
    {"events": "app_open"}, {"events": {"name": "app_open"}}, {"nope": []}, "app_open", 5, None,
    {"events": [5]}, {"events": [None]}, {"events": [{"name": "app_open", "props": [1]}]},
    {"events": [{"name": "app_open", "props": "x"}]},
    {"events": [{"name": "app_open", "ts": True}]}, {"events": [{"name": "app_open", "ts": "yesterday"}]},
    {"events": [{"name": "app_open", "ts": 12345}]}, {"events": [{"name": "app_open", "ts": [1]}]},
    {"events": [{"name": "app_open", "props": {"blob": "x" * 5000}}]},
])
async def test_validation_errors(engage, body):
    assert_error(await engage.post("/api/events", json=body, headers=dev(1)), 422, "validation_error")
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0


async def test_props_at_the_cap_are_accepted(engage):
    assert (await post(engage, [ev(blob="x" * 1900)])).status_code == 200


async def test_malformed_and_nul_json(engage):
    h = {**dev(1), "content-type": "application/json"}
    assert_error(await engage.post("/api/events", content=b'{"events": [', headers=h), 422, "validation_error")
    nul = json.dumps({"events": [{"name": "app_open", "props": {"a": "x\u0000y"}}]}).encode()
    assert_error(await engage.post("/api/events", content=nul, headers=h), 422, "validation_error")
    deep = b"[" * 5000 + b"]" * 5000
    assert_error(await engage.post("/api/events", content=deep, headers=h), 422, "validation_error")


async def test_oversized_body_413(engage):
    r = await engage.post("/api/events", content=b'{"events": [], "pad": "' + b"x" * 300_000 + b'"}',
                          headers={**dev(1), "content-type": "application/json"})
    assert_error(r, 413, "payload_too_large")


# ---------------------------------------------------------------- auth, limit, bots, 500

async def test_401(engage):
    assert_error(await engage.post("/api/events", json={"events": [ev()]}), 401, "unauthorized")


async def test_429_after_10_batches_a_minute(engage_factory):
    c = await engage_factory(rate_limit_enabled=True)
    for i in range(10):
        assert (await post(c, [ev()])).status_code == 200, i
    r = await post(c, [ev()])
    err = assert_error(r, 429, "rate_limited")
    assert int(r.headers["retry-after"]) >= 1 and err["details"]["limit"] == 10
    assert (await post(c, [ev()], tg=2)).status_code == 200
    assert await c.db.fetchval("SELECT count(*) FROM events") == 11


async def test_bots_never_produce_events(engage):
    await engage.get("/api/me", headers=dev(5))
    await engage.db.execute("UPDATE users SET is_bot = true WHERE tg_id = 5")
    assert (await post(engage, [ev()], tg=5)).json() == {"accepted": 0}
    assert (await post(engage, [ev("nope")], tg=5)).status_code == 422  # still validated like anybody else
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0
    assert (await post(engage, [ev()], tg=6)).json() == {"accepted": 1}


async def test_500_is_the_unified_error(engage, monkeypatch):
    async def boom(self, user, payload):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(events_mod.EventService, "ingest", boom)
    err = assert_error(await post(engage, [ev()]), 500, "internal_error")
    assert "secret" not in json.dumps(err)


async def test_insert_failure_stores_nothing(engage, monkeypatch):
    """A failing write rolls the whole batch back (one transaction): the 2nd row violates NOT NULL."""
    rows = lambda p, n: [{"name": "app_open", "props": {}, "ts": n}, {"name": None, "props": {}, "ts": n}]  # noqa: E731
    monkeypatch.setattr(events_mod, "parse_batch", rows)
    assert_error(await post(engage, [ev()]), 500, "internal_error")
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0
