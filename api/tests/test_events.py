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
    assert r.status_code == 200 and r.json() == {"accepted": 3, "rejected": 0}
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
    assert (await post(engage, [ev() for _ in range(50)])).json() == {"accepted": 50, "rejected": 0}
    assert (await post(engage, [])).json() == {"accepted": 0, "rejected": 0}
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 50


async def test_envelopes_and_timestamp_formats(engage):
    iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    r = await engage.post("/api/events", json=[ev(), {"name": "app_open", "ts": iso}, {"name": "app_open"},
                                                {"name": "app_open", "ts": "2026-01-01T10:00:00"}], headers=dev(1))
    assert r.json() == {"accepted": 4, "rejected": 0}  # bare list, ISO string, naive = UTC, missing ts = now
    rows = await stored(engage)
    assert rows[3]["ts"] == datetime(2026, 1, 1, 10, tzinfo=timezone.utc)
    assert abs((rows[2]["ts"] - datetime.now(timezone.utc)).total_seconds()) < 30


async def test_old_events_from_an_offline_device_are_fine(engage):
    assert (await post(engage, [ev(ts=now_ms(-3 * 86400))])).status_code == 200


async def test_future_within_the_skew_is_fine(engage):
    assert (await post(engage, [ev(ts=now_ms(30))])).status_code == 200


async def test_unknown_extra_fields_are_ignored(engage):
    e = {**ev(), "session": "abc", "id": 7}
    assert (await post(engage, [e])).json() == {"accepted": 1, "rejected": 0}
    assert (await stored(engage))[0]["props"] == {}


# ---------------------------------------------------------------- 422

async def test_too_many_events(engage):
    err = assert_error(await post(engage, [ev() for _ in range(51)]), 422, "too_many_events")
    assert err["details"] == {"max": 50, "got": 51}
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0


# ---------------------------------------------------------------- S13: partial accept (a bad item never costs the batch)

@pytest.mark.parametrize("name", ["hack", "APP_OPEN", "app_open ", "", None, 5, ["app_open"], "x" * 500])
async def test_unknown_event_name_is_dropped_the_rest_is_kept(engage, name):
    r = await post(engage, [ev(), {"name": name, "ts": now_ms()}, ev("game_start")])
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 2 and body["rejected"] == 1
    assert body["rejects"] == [{"index": 1, "reason": "invalid_event_name",
                                **({"name": name} if isinstance(name, str) and len(name) <= 64 else {})}]
    assert [x["name"] for x in await stored(engage)] == ["app_open", "game_start"]


async def test_missing_name_and_non_objects_are_dropped(engage):
    r = await post(engage, [{"ts": now_ms()}, 5, None, "app_open", ev()])
    assert r.json()["accepted"] == 1 and r.json()["rejected"] == 4
    assert [x["reason"] for x in r.json()["rejects"]] == ["invalid_event_name", "invalid_event", "invalid_event", "invalid_event"]
    assert len(await stored(engage)) == 1


async def test_future_timestamp_is_dropped_per_event(engage):
    iso = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    r = await engage.post("/api/events", json={"events": [ev(), ev("game_start", ts=now_ms(120)),
                                                          {"name": "app_open", "ts": iso}]}, headers=dev(1))
    body = r.json()
    assert r.status_code == 200 and body["accepted"] == 1 and body["rejected"] == 2
    assert [(x["index"], x["reason"]) for x in body["rejects"]] == [(1, "future_timestamp"), (2, "future_timestamp")]
    assert [x["name"] for x in await stored(engage)] == ["app_open"]


@pytest.mark.parametrize("bad, reason", [
    ({"name": "app_open", "props": [1]}, "invalid_props"), ({"name": "app_open", "props": "x"}, "invalid_props"),
    ({"name": "app_open", "ts": True}, "invalid_ts"), ({"name": "app_open", "ts": "yesterday"}, "invalid_ts"),
    ({"name": "app_open", "ts": 12345}, "invalid_ts"), ({"name": "app_open", "ts": [1]}, "invalid_ts"),
    ({"name": "app_open", "props": {"blob": "x" * 5000}}, "props_too_large"),
])
async def test_bad_ts_and_props_are_dropped_per_event(engage, bad, reason):
    r = await post(engage, [ev(), bad])
    assert r.status_code == 200 and r.json()["accepted"] == 1 and r.json()["rejects"][0]["reason"] == reason
    assert len(await stored(engage)) == 1


async def test_everything_rejected_is_still_a_200(engage):
    r = await post(engage, [ev("nope"), ev("nope2")])
    assert r.status_code == 200 and r.json()["accepted"] == 0 and r.json()["rejected"] == 2
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0


async def test_future_boundary_uses_the_clock(engage, monkeypatch):
    fixed = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(events_mod, "utcnow", lambda: fixed)
    ms = int(fixed.timestamp() * 1000)
    assert (await post(engage, [ev(ts=ms + 60_000)])).status_code == 200  # exactly 60 s ahead
    r = await post(engage, [ev(ts=ms + 60_001)])
    assert r.status_code == 200 and r.json()["accepted"] == 0 and r.json()["rejects"][0]["reason"] == "future_timestamp"


@pytest.mark.parametrize("body", [
    {"events": "app_open"}, {"events": {"name": "app_open"}}, {"nope": []}, "app_open", 5, None,
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


async def test_429_after_16_batches_a_minute(engage_factory):
    """10/min + burst 6: the web client flushes every 5 s (12/min) and once more when the tab hides."""
    c = await engage_factory(rate_limit_enabled=True)
    for i in range(16):
        assert (await post(c, [ev()])).status_code == 200, i
    r = await post(c, [ev()])
    err = assert_error(r, 429, "rate_limited")
    assert int(r.headers["retry-after"]) >= 1 and err["details"]["limit"] == 16
    assert (await post(c, [ev()], tg=2)).status_code == 200
    assert await c.db.fetchval("SELECT count(*) FROM events") == 17


async def test_bots_never_produce_events(engage):
    await engage.get("/api/me", headers=dev(5))
    await engage.db.execute("UPDATE users SET is_bot = true WHERE tg_id = 5")
    assert (await post(engage, [ev()], tg=5)).json() == {"accepted": 0, "rejected": 0}
    r = await post(engage, [ev("nope")], tg=5)  # still validated like anybody else
    assert r.json()["accepted"] == 0 and r.json()["rejected"] == 1
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0
    assert (await post(engage, [ev()], tg=6)).json() == {"accepted": 1, "rejected": 0}


async def test_500_is_the_unified_error(engage, monkeypatch):
    async def boom(self, user, payload):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(events_mod.EventService, "ingest", boom)
    err = assert_error(await post(engage, [ev()]), 500, "internal_error")
    assert "secret" not in json.dumps(err)


async def test_insert_failure_stores_nothing(engage, monkeypatch):
    """A failing write rolls the whole batch back (one transaction): the 2nd row violates NOT NULL."""
    rows = lambda p, n: [{"name": "app_open", "props": {}, "ts": n}, {"name": None, "props": {}, "ts": n}]  # noqa: E731
    monkeypatch.setattr(events_mod, "parse_batch", lambda p, n: (rows(p, n), []))
    assert_error(await post(engage, [ev()]), 500, "internal_error")
    assert await engage.db.fetchval("SELECT count(*) FROM events") == 0
