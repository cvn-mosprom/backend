"""Auth: initData verification, dev mode, user upsert, Redis cache."""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone

import pytest

from app import auth
from app.auth import verify_init_data
from app.errors import AppError
from app.settings import Settings
from app.testing import make_init_data
from tests.helpers import BOT_TOKEN, dev, tma
from tests.test_errors import assert_error

USER = {"id": 424242, "first_name": "Ann", "last_name": "Lee", "username": "ann", "language_code": "ru"}


# ------------------------------------------------------------ pure verification

def test_verify_valid_init_data():
    raw = make_init_data(BOT_TOKEN, USER, start_param="uni_msu")
    ident = verify_init_data(raw, BOT_TOKEN, 86400)
    assert (ident.tg_id, ident.first_name, ident.last_name, ident.username) == (424242, "Ann", "Lee", "ann")
    assert ident.start_param == "uni_msu"


def test_verify_handles_unicode_and_extra_fields():
    user = {"id": 5, "first_name": "Вячеслав", "username": None}
    raw = make_init_data(BOT_TOKEN, user, signature="sig", chat_type="sender", foo="bar baz")
    assert verify_init_data(raw, BOT_TOKEN, 86400).first_name == "Вячеслав"


def _code(raw: str, **kw) -> str:
    with pytest.raises(AppError) as exc:
        verify_init_data(raw, kw.pop("token", BOT_TOKEN), 86400, **kw)
    return exc.value.code


def test_verify_rejects_bad_signatures():
    raw = make_init_data(BOT_TOKEN, USER)
    assert _code(raw, token="999:OTHER") == "invalid_init_data"  # signed with another bot
    assert _code(raw.replace("Ann", "Eve")) == "invalid_init_data"  # tampered payload
    assert _code(raw[:-1] + ("0" if raw[-1] != "0" else "1")) == "invalid_init_data"  # tampered hash
    assert _code(raw.split("&hash=")[0]) == "invalid_init_data"  # no hash


@pytest.mark.parametrize("raw", ["", "garbage", "a=1&a=2&hash=00", "hash=", "&&&"])
def test_verify_rejects_malformed(raw):
    assert _code(raw) == "invalid_init_data"


def test_verify_requires_user_and_auth_date():
    import hmac
    from urllib.parse import urlencode

    def sign(fields: dict) -> str:
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})

    now = str(int(time.time()))
    assert _code(sign({"auth_date": now})) == "invalid_init_data"  # no user
    assert _code(sign({"user": '{"id":1}'})) == "invalid_init_data"  # no auth_date
    assert _code(sign({"auth_date": now, "user": "not json"})) == "invalid_init_data"
    assert _code(sign({"auth_date": now, "user": '{"id":"x"}'})) == "invalid_init_data"
    assert _code(sign({"auth_date": now, "user": '{"id":-3}'})) == "invalid_init_data"
    assert _code(sign({"auth_date": "soon", "user": '{"id":1}'})) == "invalid_init_data"


def test_verify_auth_date_window():
    old = make_init_data(BOT_TOKEN, USER, auth_date=int(time.time()) - 86401)
    assert _code(old) == "auth_expired"
    edge = make_init_data(BOT_TOKEN, USER, auth_date=int(time.time()) - 86000)
    assert verify_init_data(edge, BOT_TOKEN, 86400).tg_id == 424242
    future = make_init_data(BOT_TOKEN, USER, auth_date=int(time.time()) + 3600)
    assert _code(future) == "invalid_init_data"
    # expiry is checked after the signature: a forged old payload is invalid, not "expired"
    assert _code(old.replace("Ann", "Eve")) == "invalid_init_data"


def test_prod_forces_dev_auth_off():
    assert Settings(app_env="prod", dev_auth=True).dev_auth_enabled is False
    assert Settings(app_env="dev", dev_auth=True).dev_auth_enabled is True
    assert Settings(app_env="dev", dev_auth=False).dev_auth_enabled is False
    assert Settings(app_env=" PROD ").is_prod


# ------------------------------------------------------------ HTTP error paths (no DB needed)

async def test_missing_authorization_is_401_unauthorized(make_client):
    c = await make_client()
    assert_error(await c.get("/api/me"), 401, "unauthorized")


@pytest.mark.parametrize("header", ["Bearer abc", "tma", "Basic Zm9vOmJhcg==", "dev"])
async def test_unusable_authorization_headers(make_client, header):
    c = await make_client()
    resp = await c.get("/api/me", headers={"Authorization": header})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] in {"unauthorized", "invalid_init_data"}


async def test_bad_hash_is_401_invalid_init_data(make_client):
    c = await make_client()
    bad = make_init_data(BOT_TOKEN, USER, auth_date=int(time.time()))
    bad = bad.replace("Ann", "Mallory")
    assert_error(await c.get("/api/me", headers={"Authorization": f"tma {bad}"}), 401, "invalid_init_data")
    wrong_bot = {"Authorization": "tma " + make_init_data("1:other-token", USER)}
    assert_error(await c.get("/api/me", headers=wrong_bot), 401, "invalid_init_data")
    assert_error(await c.get("/api/me", headers={"Authorization": "tma not-init-data"}), 401, "invalid_init_data")


async def test_old_auth_date_is_401_auth_expired(make_client):
    c = await make_client()
    resp = await c.get("/api/me", headers=tma(USER, auth_date=int(time.time()) - 90000))
    err = assert_error(resp, 401, "auth_expired")
    assert err["details"] == {"max_age_s": 86400}
    # INIT_DATA_MAX_AGE_S is honoured
    c2 = await make_client(init_data_max_age_s=60)
    assert_error(await c2.get("/api/me", headers=tma(USER, auth_date=int(time.time()) - 120)), 401, "auth_expired")


async def test_dev_auth_disabled_in_prod(make_client):
    c = await make_client(app_env="prod", dev_auth=True)
    assert_error(await c.get("/api/me", headers=dev(5)), 401, "unauthorized")
    assert c.app.state.settings.dev_auth_enabled is False


async def test_dev_auth_disabled_by_flag(make_client):
    c = await make_client(dev_auth=False)
    assert_error(await c.get("/api/me", headers=dev(5)), 401, "unauthorized")


async def test_dev_auth_malformed(make_client):
    c = await make_client()
    for value in ("dev abc", "dev 0", "dev -4", "dev ", "dev 9223372036854775808", "dev " + "9" * 23, "dev " + "9" * 5000):
        assert_error(await c.get("/api/me", headers={"Authorization": value}), 401, "invalid_init_data")


def test_tg_id_must_fit_a_signed_bigint():
    assert auth.parse_dev_header(str(2**63 - 1)).tg_id == 2**63 - 1
    assert verify_init_data(make_init_data(BOT_TOKEN, {"id": 2**63 - 1}), BOT_TOKEN, 86400).tg_id == 2**63 - 1
    for too_big in ("9223372036854775808", "9" * 23, "9" * 5000):
        with pytest.raises(AppError) as exc:
            auth.parse_dev_header(too_big)
        assert exc.value.code == "invalid_init_data"
    for too_big in (2**63, 10**23, 1e30, float("inf")):  # inf: json "Infinity", int() raises OverflowError
        assert _code(make_init_data(BOT_TOKEN, {"id": too_big})) == "invalid_init_data"


async def test_tma_without_bot_token_is_config_error(make_client):
    c = await make_client(bot_token="")
    assert_error(await c.get("/api/me", headers=tma(USER)), 503, "config_error")


# ------------------------------------------------------------ with Postgres (+ Redis)

async def test_valid_init_data_creates_user_once(live):
    h = tma(USER, start_param="uni_msu")
    first = await live.get("/api/me", headers=h)
    assert first.status_code == 200, first.text
    body = first.json()
    assert set(body) == {"id", "tgId", "name", "username", "ageConfirmed", "createdAt"}
    assert body["tgId"] == 424242 and body["name"] == "Ann Lee" and body["username"] == "ann"
    assert body["ageConfirmed"] is False
    assert datetime.fromisoformat(body["createdAt"]).tzinfo is not None

    again = await live.get("/api/me", headers=tma(USER))  # a fresh signature, same person
    assert again.status_code == 200 and again.json()["id"] == body["id"]
    assert again.json()["createdAt"] == body["createdAt"]
    rows = await live.db.fetch("SELECT tg_id, source, is_bot, age_confirmed FROM users")
    assert [tuple(r) for r in rows] == [(424242, "uni_msu", False, False)]


async def test_access_log_carries_user_id(live, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        me = (await live.get("/api/me", headers=dev(55, "Logged"))).json()
    rec = [r for r in caplog.records if r.name == "mosprom.access"][-1].fields
    assert rec["user_id"] == me["id"] and rec["tg_id"] == 55
    assert rec["status"] == 200 and rec["path"] == "/api/me" and rec["error_code"] is None


async def test_dev_auth_creates_user(live):
    resp = await live.get("/api/me", headers=dev(77, "Dana"))
    assert resp.status_code == 200 and resp.json()["name"] == "Dana" and resp.json()["tgId"] == 77
    assert (await live.get("/api/me", headers=dev(77))).json()["id"] == resp.json()["id"]
    assert (await live.get("/api/me", headers=dev(78))).json()["name"] == "Dev78"
    assert await live.db.fetchval("SELECT count(*) FROM users") == 2


async def test_upsert_refreshes_profile_and_last_seen(live):
    await live.get("/api/me", headers=tma({"id": 9, "first_name": "Old", "username": "old"}, start_param="a"))
    await live.db.execute("UPDATE users SET last_seen_at = now() - interval '1 hour' WHERE tg_id = 9")
    before = await live.db.fetchval("SELECT last_seen_at FROM users WHERE tg_id = 9")

    resp = await live.get("/api/me", headers=tma({"id": 9, "first_name": "New", "username": "new"}, start_param="b"))
    assert resp.json()["name"] == "New" and resp.json()["username"] == "new"
    row = await live.db.fetchrow("SELECT first_name, username, source, last_seen_at FROM users WHERE tg_id = 9")
    assert (row["first_name"], row["username"]) == ("New", "new")
    assert row["source"] == "a"  # the first start_param sticks
    assert row["last_seen_at"] > before
    assert (datetime.now(timezone.utc) - row["last_seen_at"]).total_seconds() < 30


async def test_last_seen_write_is_throttled(live):
    await live.get("/api/me", headers=dev(11, "Zed"))
    t1 = await live.db.fetchval("SELECT last_seen_at FROM users WHERE tg_id = 11")
    await live.get("/api/me", headers=dev(11, "Zed"))
    assert await live.db.fetchval("SELECT last_seen_at FROM users WHERE tg_id = 11") == t1


async def test_concurrent_first_requests_create_one_row(live):
    responses = await asyncio.gather(*(live.get("/api/me", headers=dev(31337, "Racer")) for _ in range(12)))
    assert {r.status_code for r in responses} == {200}
    assert len({r.json()["id"] for r in responses}) == 1
    assert await live.db.fetchval("SELECT count(*) FROM users WHERE tg_id = 31337") == 1


async def test_verified_init_data_is_cached_in_redis(live, monkeypatch):
    calls = 0
    real = auth.verify_init_data

    def counting(*a, **kw):
        nonlocal calls
        calls += 1
        return real(*a, **kw)

    monkeypatch.setattr(auth, "verify_init_data", counting)
    h = tma(USER)
    for _ in range(3):
        assert (await live.get("/api/me", headers=h)).status_code == 200
    assert calls == 1
    key = "auth:" + hashlib.sha256(h["Authorization"][4:].encode()).hexdigest()
    assert 0 < await live.redis.ttl(key) <= 300


async def test_auth_cache_does_not_extend_validity(live):
    h = tma(USER, auth_date=int(time.time()) - 100)
    assert (await live.get("/api/me", headers=h)).status_code == 200  # cached now
    live.app.state.settings.init_data_max_age_s = 50  # tighten: the cached identity is 100 s old
    assert_error(await live.get("/api/me", headers=h), 401, "auth_expired")


async def test_invalid_init_data_is_not_cached(live):
    bad = {"Authorization": "tma " + make_init_data(BOT_TOKEN, USER).replace("Ann", "Eve")}
    assert_error(await live.get("/api/me", headers=bad), 401, "invalid_init_data")
    assert [k async for k in live.redis.scan_iter("auth:*")] == []


async def test_auth_works_with_redis_down(make_client, clean_db):
    from tests.helpers import TEST_DB_URL

    c = await make_client(database_url=TEST_DB_URL)  # Redis stub is down
    assert (await c.get("/api/me", headers=tma(USER))).status_code == 200
    assert (await c.get("/api/me", headers=dev(3))).status_code == 200
