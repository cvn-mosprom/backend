"""Redis fixed-window rate limiter."""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.db import make_redis
from app.errors import AppError
from app.ratelimit import LIMITS, enforce, rate_limit
from tests.helpers import TEST_REDIS_URL, dev
from tests.test_errors import assert_error


def test_limit_table_matches_spec():
    """Spec "Блок 7": per user per minute."""
    per_min = {name: lim.limit for name, lim in LIMITS.items()}
    assert per_min["bootstrap"] == 10 and per_min["factory_me"] == 60 and per_min["command"] == 120
    assert per_min["advance"] == 10 and per_min["leaderboard"] == 60 and per_min["events"] == 10
    assert LIMITS["events"].max_requests >= 13  # the client flushes every 5 s = 12/min (S13)
    assert per_min["leads"] == 5 and per_min["quiz"] == 30
    assert all(lim.window_s == 60 for lim in LIMITS.values())
    assert LIMITS["command"].burst > 0  # "small burst"


def test_unknown_limit_name_fails_at_declaration():
    with pytest.raises(KeyError):
        rate_limit("nope")
    assert callable(rate_limit("command"))


async def test_429_after_limit_with_retry_after(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 3})
    for _ in range(3):
        assert (await c.get("/t/limited", headers=dev(1))).status_code == 200
    resp = await c.get("/t/limited", headers=dev(1))
    err = assert_error(resp, 429, "rate_limited")
    retry_after = int(resp.headers["retry-after"])
    assert 1 <= retry_after <= 60
    assert err["details"] == {"retry_after": retry_after, "limit": 3, "window_s": 60}
    # keeps rejecting inside the window
    assert (await c.get("/t/limited", headers=dev(1))).status_code == 429


async def test_limit_is_per_user(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 2})
    for _ in range(2):
        assert (await c.get("/t/limited", headers=dev(1))).status_code == 200
    assert (await c.get("/t/limited", headers=dev(1))).status_code == 429
    assert (await c.get("/t/limited", headers=dev(2))).status_code == 200  # another user is unaffected


async def test_unauthenticated_requests_are_limited_per_ip(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 2})
    for _ in range(2):  # the limiter counts them; the route itself is public here
        assert (await c.get("/t/limited")).status_code == 200
    assert_error(await c.get("/t/limited"), 429, "rate_limited")
    keys = [k async for k in clean_redis.scan_iter("rl:*")]
    assert len(keys) == 1 and keys[0].startswith("rl:default:ip:")


async def test_rejected_auth_still_counts_against_ip(make_client, clean_redis):
    """Auth failures on a limited route are throttled per IP, then answered 429 instead of 401."""
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 2})
    for _ in range(2):
        assert_error(await c.get("/api/me"), 401, "unauthorized")
    assert_error(await c.get("/api/me"), 429, "rate_limited")


async def test_counter_key_expires_and_window_resets(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 1})
    assert (await c.get("/t/limited", headers=dev(5))).status_code == 200
    assert (await c.get("/t/limited", headers=dev(5))).status_code == 429
    (key,) = [k async for k in clean_redis.scan_iter("rl:*")]
    assert 0 < await clean_redis.ttl(key) <= 60  # never an immortal counter
    await clean_redis.pexpire(key, 50)
    await asyncio.sleep(0.15)
    assert (await c.get("/t/limited", headers=dev(5))).status_code == 200


async def test_burst_is_allowed_for_command(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL)

    class Req:  # minimal stand-in: enforce() needs app.state and an identity
        app = c.app
        client = None

        class state:  # noqa: N801
            identity = type("I", (), {"tg_id": 99})()

    n = LIMITS["command"].max_requests
    for _ in range(n):
        await enforce(Req, "command")
    with pytest.raises(AppError) as exc:
        await enforce(Req, "command")
    assert exc.value.code == "rate_limited" and exc.value.status == 429
    assert exc.value.headers["Retry-After"]


async def test_limiter_fails_open_when_redis_is_down(make_client, caplog):
    c = await make_client(rate_limit_overrides={"default": 1})  # Redis stub raises on every call
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert (await c.get("/t/limited", headers=dev(1))).status_code == 200
    assert any("failing open" in r.getMessage() for r in caplog.records)


async def test_limiter_fails_open_on_unreachable_redis_server(make_client):
    """A real client with a closed port (not a stub): still no 500."""
    c = await make_client(redis_url="redis://127.0.0.1:1/0", rate_limit_overrides={"default": 1})
    c.app.state.redis = make_redis(c.app.state.settings)
    for _ in range(2):
        assert (await c.get("/t/limited", headers=dev(1))).status_code == 200


async def test_limiter_can_be_disabled(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 1}, rate_limit_enabled=False)
    for _ in range(3):
        assert (await c.get("/t/limited", headers=dev(1))).status_code == 200
    assert [k async for k in clean_redis.scan_iter("rl:*")] == []


async def test_me_endpoint_is_rate_limited_per_user(live):
    live.app.state.settings.rate_limit_overrides = {"default": 3}
    for _ in range(3):
        assert (await live.get("/api/me", headers=dev(8))).status_code == 200
    resp = await live.get("/api/me", headers=dev(8))
    assert_error(resp, 429, "rate_limited")
    assert resp.headers["retry-after"]
    assert (await live.get("/api/me", headers=dev(9))).status_code == 200
