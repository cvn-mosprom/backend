"""S14: web login via the Telegram bot (codes, deep link, bearer sessions). Real Postgres + Redis (skipped when absent)."""
from __future__ import annotations

import asyncio
import hashlib

import pytest
import pytest_asyncio

from app import webauth
from tests.game_helpers import bootstrap
from tests.helpers import INTERNAL_TOKEN, TEST_DB_URL, TEST_REDIS_URL, dev
from tests.test_errors import assert_error

TOKEN_H = {"X-Internal-Token": INTERNAL_TOKEN}
TG = 777001
TG2 = 777002
BOT = "mosprom_test_bot"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def web(make_client, clean_db, clean_redis, fake_sim):
    """Client on the throwaway Postgres + Redis with the rate limiter OFF and BOT_USERNAME set."""
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                          rate_limit_enabled=False, bot_username=BOT)
    c.db, c.redis = clean_db, clean_redis
    return c


@pytest_asyncio.fixture
async def limited(make_client, clean_db, clean_redis, fake_sim):
    """Same, with the real rate limiter (defaults: auth 5/min)."""
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, bot_username=BOT)
    c.db, c.redis = clean_db, clean_redis
    return c


async def login_code(c, tg: int = TG, **names) -> str:
    r = await c.post("/internal/auth/login-code", json={"tg_id": tg, **names}, headers=TOKEN_H)
    assert r.status_code == 200, r.text
    return r.json()["code"]


async def code_login(c, tg: int = TG, **names) -> dict:
    code = await login_code(c, tg, **names)
    r = await c.post("/api/auth/web/code", json={"code": code})
    assert r.status_code == 200, r.text
    return r.json()


async def deeplink_login(c, tg: int = TG) -> dict:
    nonce = (await c.post("/api/auth/web/start")).json()["nonce"]
    r = await c.post("/internal/auth/approve", json={"nonce": nonce, "tg_id": tg}, headers=TOKEN_H)
    assert r.status_code == 200, r.text
    r = await c.post("/api/auth/web/poll", json={"nonce": nonce})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- schema / config

async def test_migration_creates_tables(web):
    names = {r["tablename"] for r in await web.db.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"web_login_codes", "web_login_requests", "web_sessions"} <= names
    assert await web.db.fetchval("SELECT version_num FROM alembic_version") >= "0011"


async def test_cors_preflight_allows_authorization(web):
    r = await web.options("/api/auth/web/session", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "DELETE",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert r.status_code == 200
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "DELETE" in r.headers["access-control-allow-methods"]


# ---------------------------------------------------------------- internal endpoints: token

@pytest.mark.parametrize("path, body", [
    ("/internal/auth/login-code", {"tg_id": TG}),
    ("/internal/auth/approve", {"nonce": "x", "tg_id": TG}),
    ("/internal/auth/revoke-all", {"tg_id": TG}),
])
async def test_internal_needs_token(web, path, body):
    assert_error(await web.post(path, json=body), 401, "unauthorized")
    assert_error(await web.post(path, json=body, headers={"X-Internal-Token": "nope"}), 401, "unauthorized")
    assert await web.db.fetchval("SELECT count(*) FROM users") == 0  # nothing was created


async def test_internal_validation(web):
    assert_error(await web.post("/internal/auth/login-code", json={"tg_id": 0}, headers=TOKEN_H), 422, "validation_error")
    assert_error(await web.post("/internal/auth/login-code", json={}, headers=TOKEN_H), 422, "validation_error")
    assert_error(await web.post("/internal/auth/login-code", json={"tg_id": 2**63}, headers=TOKEN_H), 422,
                 "validation_error")


# ---------------------------------------------------------------- flow 1: code

async def test_login_code_creates_user_and_stores_only_a_hash(web):
    r = await web.post("/internal/auth/login-code", headers=TOKEN_H, json={
        "tg_id": TG, "username": "ann", "first_name": "Ann", "last_name": "Lee"})
    assert r.status_code == 200
    body = r.json()
    code = body["code"]
    assert len(code) == 6 and code.isdigit() and set(body) == {"code", "expiresAt"}
    u = await web.db.fetchrow("SELECT * FROM users WHERE tg_id = $1", TG)
    assert (u["username"], u["first_name"], u["last_name"], u["is_test"], u["is_bot"]) == ("ann", "Ann", "Lee", False, False)
    row = await web.db.fetchrow("SELECT * FROM web_login_codes")
    assert row["code_hash"] == sha(code) and code not in str(dict(row))
    ttl = (row["expires_at"] - row["created_at"]).total_seconds()
    assert 299 <= ttl <= 301 and row["used_at"] is None


async def test_login_code_camel_case_body_and_profile_refresh(web):
    await login_code(web, TG, username="old", first_name="Old")
    r = await web.post("/internal/auth/login-code", headers=TOKEN_H,
                       json={"tgId": TG, "username": "new", "firstName": "New", "lastName": "Name"})
    assert r.status_code == 200
    u = await web.db.fetchrow("SELECT * FROM users WHERE tg_id = $1", TG)
    assert (u["username"], u["first_name"], u["last_name"]) == ("new", "New", "Name")
    await login_code(web, TG)  # a bare {tg_id} keeps the stored profile
    u = await web.db.fetchrow("SELECT * FROM users WHERE tg_id = $1", TG)
    assert (u["username"], u["first_name"]) == ("new", "New")


async def test_code_login_returns_token_and_bearer_works(web):
    s = await code_login(web, TG, username="ann", first_name="Ann", last_name="Lee")
    assert set(s) == {"token", "expiresAt", "user"} and len(s["token"]) >= 40
    assert s["user"]["tgId"] == TG and s["user"]["name"] == "Ann Lee" and s["user"]["username"] == "ann"
    row = await web.db.fetchrow("SELECT * FROM web_sessions")
    assert row["token_hash"] == sha(s["token"]) and s["token"] not in str(dict(row))
    days = (row["expires_at"] - row["created_at"]).total_seconds() / 86400
    assert 29.9 < days < 30.1
    g = await web.get("/api/auth/web/session", headers=bearer(s["token"]))
    assert g.status_code == 200 and g.json()["user"] == s["user"] and g.json()["expiresAt"]
    assert (await web.get("/api/me", headers=bearer(s["token"]))).json()["tgId"] == TG


async def test_bearer_reaches_the_same_factory_as_dev_auth(web):
    s = await code_login(web, TG, first_name="Ann")
    dev_boot = await bootstrap(web, TG)  # the Telegram / dev side opens the game first
    r = await web.get("/api/bootstrap", headers=bearer(s["token"]))
    assert r.status_code == 200, r.text
    web_boot = r.json()
    assert web_boot["version"] == dev_boot["version"] and web_boot["season"]["id"] == dev_boot["season"]["id"]
    keys = ("name", "credits", "level")
    assert {k: web_boot["snapshot"]["factory"][k] for k in keys} == {k: dev_boot["snapshot"]["factory"][k] for k in keys}
    assert await web.db.fetchval("SELECT count(*) FROM factories") == 1
    assert await web.db.fetchval("SELECT count(*) FROM users WHERE tg_id = $1", TG) == 1
    me_dev = (await web.get("/api/factory/me", headers=dev(TG))).json()
    me_web = (await web.get("/api/factory/me", headers=bearer(s["token"]))).json()
    assert me_dev["version"] == me_web["version"] and me_dev["snapshot"]["player"] == me_web["snapshot"]["player"]
    assert me_web["snapshot"]["player"]["tgId"] == TG


async def test_bearer_first_then_telegram_side_sees_the_same_user(web):
    """The web side may bootstrap first: the factory is created for the bot-made users row, dev auth finds it."""
    s = await code_login(web, TG, first_name="Ann")
    assert (await web.get("/api/bootstrap", headers=bearer(s["token"]))).status_code == 200
    assert (await web.get("/api/factory/me", headers=dev(TG))).status_code == 200
    assert await web.db.fetchval("SELECT count(*) FROM users") == 1
    assert await web.db.fetchval("SELECT count(*) FROM factories") == 1


@pytest.mark.parametrize("code", ["000000", "12345", "1234567", "abcdef", "12 34", "", "١٢٣٤٥٦"])
async def test_wrong_or_malformed_code_is_401(web, code):
    await login_code(web, TG)
    assert_error(await web.post("/api/auth/web/code", json={"code": code}), 401, "invalid_code")
    assert await web.db.fetchval("SELECT count(*) FROM web_sessions") == 0


async def test_code_body_validation(web):
    assert_error(await web.post("/api/auth/web/code", json={}), 422, "validation_error")
    assert_error(await web.post("/api/auth/web/code", json={"code": 123456}), 422, "validation_error")


async def test_code_with_spaces_is_accepted(web):
    code = await login_code(web)
    r = await web.post("/api/auth/web/code", json={"code": f" {code[:3]} {code[3:]} "})
    assert r.status_code == 200


async def test_code_is_single_use(web):
    code = await login_code(web)
    assert (await web.post("/api/auth/web/code", json={"code": code})).status_code == 200
    assert_error(await web.post("/api/auth/web/code", json={"code": code}), 401, "invalid_code")
    assert await web.db.fetchval("SELECT count(*) FROM web_sessions") == 1


async def test_code_redeemed_concurrently_gives_one_session(web):
    code = await login_code(web)
    rs = await asyncio.gather(*[web.post("/api/auth/web/code", json={"code": code}) for _ in range(6)])
    assert sorted(r.status_code for r in rs) == [200] + [401] * 5
    assert await web.db.fetchval("SELECT count(*) FROM web_sessions") == 1


async def test_expired_code_is_401(web):
    code = await login_code(web)
    await web.db.execute("UPDATE web_login_codes SET expires_at = now() - interval '1 second'")
    assert_error(await web.post("/api/auth/web/code", json={"code": code}), 401, "invalid_code")


async def test_new_code_invalidates_older_unused_ones(web):
    first = await login_code(web, TG)
    other = await login_code(web, TG2)
    second = await login_code(web, TG)
    assert first != second
    assert_error(await web.post("/api/auth/web/code", json={"code": first}), 401, "invalid_code")
    assert (await web.post("/api/auth/web/code", json={"code": second})).json()["user"]["tgId"] == TG
    assert (await web.post("/api/auth/web/code", json={"code": other})).json()["user"]["tgId"] == TG2  # another user's code lives


async def test_two_users_never_share_live_digits(web, monkeypatch):
    draws = iter(["111111", "111111", "222222"])
    monkeypatch.setattr(webauth, "new_code", lambda: next(draws))
    a = await login_code(web, TG)
    b = await login_code(web, TG2)
    assert (a, b) == ("111111", "222222")


async def test_wrong_attempts_lock_the_ip_even_for_the_right_code(web):
    code = await login_code(web)
    for _ in range(5):
        assert_error(await web.post("/api/auth/web/code", json={"code": "000000"}), 401, "invalid_code")
    r = await web.post("/api/auth/web/code", json={"code": code})
    err = assert_error(r, 429, "rate_limited")
    assert 1 <= int(r.headers["retry-after"]) <= webauth.WRONG_CODE_WINDOW_S and err["details"]["retry_after"]
    await web.redis.flushdb()  # the lock lives in Redis only; the code itself is untouched
    assert (await web.post("/api/auth/web/code", json={"code": code})).status_code == 200


async def test_success_resets_the_wrong_attempt_counter(web):
    for _ in range(4):
        assert_error(await web.post("/api/auth/web/code", json={"code": "000000"}), 401, "invalid_code")
    assert (await web.post("/api/auth/web/code", json={"code": await login_code(web)})).status_code == 200
    for _ in range(4):  # a fresh budget
        assert_error(await web.post("/api/auth/web/code", json={"code": "000000"}), 401, "invalid_code")


async def test_code_rate_limit_is_per_ip_5_per_minute(limited):
    for _ in range(5):
        assert_error(await limited.post("/api/auth/web/code", json={"code": "000000"}), 401, "invalid_code")
    r = await limited.post("/api/auth/web/code", json={"code": "000000"}, headers=dev(5))  # a header does not dodge it
    err = assert_error(r, 429, "rate_limited")
    assert err["details"]["limit"] == 5 and int(r.headers["retry-after"]) >= 1
    assert [k async for k in limited.redis.scan_iter("rl:auth:ip:*")]


async def test_start_shares_the_auth_limit(limited):
    for _ in range(5):
        assert (await limited.post("/api/auth/web/start")).status_code == 200
    assert_error(await limited.post("/api/auth/web/start"), 429, "rate_limited")


# ---------------------------------------------------------------- flow 2: deep link

async def test_start_returns_deep_link(web):
    r = await web.post("/api/auth/web/start")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == {"nonce", "botUrl", "expiresAt"}
    assert b["botUrl"] == f"https://t.me/{BOT}?start=weblogin_{b['nonce']}"
    assert len(b["nonce"]) <= 40 and all(ch.isalnum() or ch in "-_" for ch in b["nonce"])
    row = await web.db.fetchrow("SELECT * FROM web_login_requests")
    assert row["nonce_hash"] == sha(b["nonce"]) and b["nonce"] not in str(dict(row))
    assert 299 <= (row["expires_at"] - row["created_at"]).total_seconds() <= 301


async def test_start_without_bot_username_is_503(make_client, clean_db, clean_redis, fake_sim):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                          rate_limit_enabled=False, bot_username="")
    assert_error(await c.post("/api/auth/web/start"), 503, "config_error")
    # the code login keeps working
    code = (await c.post("/internal/auth/login-code", json={"tg_id": TG}, headers=TOKEN_H)).json()["code"]
    assert (await c.post("/api/auth/web/code", json={"code": code})).status_code == 200


async def test_bot_username_at_sign_is_stripped(make_client, clean_db, clean_redis, fake_sim):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                          rate_limit_enabled=False, bot_username=" @MyBot ")
    assert (await c.post("/api/auth/web/start")).json()["botUrl"].startswith("https://t.me/MyBot?start=weblogin_")


async def test_poll_pending_then_approved_token_once(web):
    nonce = (await web.post("/api/auth/web/start")).json()["nonce"]
    r = await web.post("/api/auth/web/poll", json={"nonce": nonce})
    assert r.status_code == 202 and r.json() == {"status": "pending"}
    a = await web.post("/internal/auth/approve", headers=TOKEN_H, json={
        "nonce": nonce, "tg_id": TG, "username": "ann", "first_name": "Ann"})
    assert a.status_code == 200 and a.json() == {"ok": True}
    assert await web.db.fetchval("SELECT count(*) FROM web_sessions") == 0  # no token yet: it is minted by the poll
    r = await web.post("/api/auth/web/poll", json={"nonce": nonce})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert s["user"]["tgId"] == TG and s["user"]["name"] == "Ann"
    assert (await web.get("/api/me", headers=bearer(s["token"]))).json()["tgId"] == TG
    assert_error(await web.post("/api/auth/web/poll", json={"nonce": nonce}), 410, "login_expired")  # once only
    assert await web.db.fetchval("SELECT count(*) FROM web_sessions") == 1


async def test_poll_unknown_nonce_and_bad_body(web):
    assert_error(await web.post("/api/auth/web/poll", json={"nonce": "nope"}), 410, "login_expired")
    assert_error(await web.post("/api/auth/web/poll", json={}), 422, "validation_error")
    assert_error(await web.post("/api/auth/web/poll", json={"nonce": "x" * 500}), 422, "validation_error")


async def test_expired_request_cannot_be_polled_or_approved(web):
    nonce = (await web.post("/api/auth/web/start")).json()["nonce"]
    await web.db.execute("UPDATE web_login_requests SET expires_at = now() - interval '1 second'")
    assert_error(await web.post("/api/auth/web/poll", json={"nonce": nonce}), 410, "login_expired")
    r = await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": nonce, "tg_id": TG})
    assert_error(r, 410, "login_expired")


async def test_approved_but_expired_before_collecting_is_gone(web):
    nonce = (await web.post("/api/auth/web/start")).json()["nonce"]
    await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": nonce, "tg_id": TG})
    await web.db.execute("UPDATE web_login_requests SET expires_at = now() - interval '1 second'")
    assert_error(await web.post("/api/auth/web/poll", json={"nonce": nonce}), 410, "login_expired")


async def test_approve_is_idempotent_for_the_same_user_but_not_for_another(web):
    nonce = (await web.post("/api/auth/web/start")).json()["nonce"]
    ok = {"nonce": nonce, "tg_id": TG}
    assert (await web.post("/internal/auth/approve", headers=TOKEN_H, json=ok)).status_code == 200
    assert (await web.post("/internal/auth/approve", headers=TOKEN_H, json=ok)).status_code == 200
    r = await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": nonce, "tg_id": TG2})
    assert_error(r, 410, "login_expired")
    assert (await web.post("/api/auth/web/poll", json={"nonce": nonce})).json()["user"]["tgId"] == TG


async def test_approve_unknown_nonce_is_410_and_after_collection_too(web):
    r = await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": "unknown", "tg_id": TG})
    assert_error(r, 410, "login_expired")
    nonce = (await web.post("/api/auth/web/start")).json()["nonce"]
    await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": nonce, "tg_id": TG})
    await web.post("/api/auth/web/poll", json={"nonce": nonce})
    assert_error(await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": nonce, "tg_id": TG}), 410,
                 "login_expired")


async def test_poll_concurrently_gives_one_token(web):
    nonce = (await web.post("/api/auth/web/start")).json()["nonce"]
    await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": nonce, "tg_id": TG})
    rs = await asyncio.gather(*[web.post("/api/auth/web/poll", json={"nonce": nonce}) for _ in range(5)])
    assert sorted(r.status_code for r in rs) == [200, 410, 410, 410, 410]


async def test_poll_has_its_own_generous_limit(limited):
    nonce = (await limited.post("/api/auth/web/start")).json()["nonce"]
    for _ in range(30):  # ~ 1 minute of polling every 2 s
        assert (await limited.post("/api/auth/web/poll", json={"nonce": nonce})).status_code == 202


# ---------------------------------------------------------------- bearer sessions

async def test_unknown_malformed_and_missing_bearer_is_401_unauthorized(web):
    for h in (bearer("x" * 43), bearer("x" * 1000), {"Authorization": "Bearer "}, {"Authorization": "Bearer"}):
        assert_error(await web.get("/api/auth/web/session", headers=h), 401, "unauthorized")
    assert_error(await web.get("/api/auth/web/session"), 401, "unauthorized")


async def test_expired_session_is_auth_expired(web):
    s = await code_login(web)
    await web.db.execute("UPDATE web_sessions SET expires_at = now() - interval '1 second'")
    assert_error(await web.get("/api/me", headers=bearer(s["token"])), 401, "auth_expired")


async def test_logout_revokes_this_session_only(web):
    a = await code_login(web, TG)
    b = await code_login(web, TG)
    r = await web.delete("/api/auth/web/session", headers=bearer(a["token"]))
    assert r.status_code == 204 and r.content == b""
    assert_error(await web.get("/api/me", headers=bearer(a["token"])), 401, "unauthorized")
    assert (await web.get("/api/me", headers=bearer(b["token"]))).status_code == 200
    assert_error(await web.delete("/api/auth/web/session", headers=bearer(a["token"])), 401, "unauthorized")
    assert await web.db.fetchval("SELECT count(*) FROM web_sessions WHERE revoked_at IS NOT NULL") == 1


async def test_logout_with_dev_auth_is_a_noop(web):
    s = await code_login(web, TG)
    assert (await web.delete("/api/auth/web/session", headers=dev(TG))).status_code == 204
    assert (await web.get("/api/me", headers=bearer(s["token"]))).status_code == 200
    g = (await web.get("/api/auth/web/session", headers=dev(TG))).json()
    assert g["user"]["tgId"] == TG and g["expiresAt"] is None


async def test_revoke_all_kills_every_session_code_and_pending_login(web):
    a = await code_login(web, TG)
    b = await deeplink_login(web, TG)
    other = await code_login(web, TG2)
    code = await login_code(web, TG)  # unused code
    nonce = (await web.post("/api/auth/web/start")).json()["nonce"]
    await web.post("/internal/auth/approve", headers=TOKEN_H, json={"nonce": nonce, "tg_id": TG})  # not collected yet
    r = await web.post("/internal/auth/revoke-all", headers=TOKEN_H, json={"tg_id": TG})
    assert r.status_code == 200 and r.json() == {"revoked": 2}
    for s in (a, b):
        assert_error(await web.get("/api/me", headers=bearer(s["token"])), 401, "unauthorized")
    assert_error(await web.post("/api/auth/web/code", json={"code": code}), 401, "invalid_code")
    assert_error(await web.post("/api/auth/web/poll", json={"nonce": nonce}), 410, "login_expired")
    assert (await web.get("/api/me", headers=bearer(other["token"]))).status_code == 200  # another user untouched
    r = await web.post("/internal/auth/revoke-all", headers=TOKEN_H, json={"tg_id": TG})
    assert r.json() == {"revoked": 0}
    assert (await web.post("/internal/auth/revoke-all", headers=TOKEN_H, json={"tg_id": 999})).json() == {"revoked": 0}


# ---------------------------------------------------------------- priority and sliding expiry

async def test_bearer_has_priority_over_other_headers(web):
    s = await code_login(web, TG)
    h = {**bearer(s["token"]), "X-Dev-User": "555", "X-Telegram-Init-Data": "garbage"}
    assert (await web.get("/api/me", headers=h)).json()["tgId"] == TG
    # a bad Bearer is never rescued by a dev header (the first header present wins)
    bad = {**bearer("x" * 43), "X-Dev-User": str(TG)}
    assert_error(await web.get("/api/me", headers=bad), 401, "unauthorized")
    # and a dev Authorization still works next to sessions (unchanged behaviour)
    assert (await web.get("/api/me", headers=dev(555))).json()["tgId"] == 555


async def test_sliding_expiry_is_renewed_at_most_once_per_hour(web):
    s = await code_login(web, TG)
    h = bearer(s["token"])
    before = await web.db.fetchrow("SELECT last_seen_at, expires_at FROM web_sessions")
    assert (await web.get("/api/me", headers=h)).status_code == 200  # < 1 h since creation: untouched
    assert dict(await web.db.fetchrow("SELECT last_seen_at, expires_at FROM web_sessions")) == dict(before)
    await web.db.execute("UPDATE web_sessions SET last_seen_at = now() - interval '2 hours', "
                         "expires_at = now() + interval '3 days'")
    assert (await web.get("/api/me", headers=h)).status_code == 200
    after = await web.db.fetchrow("SELECT now() AS n, last_seen_at, expires_at FROM web_sessions")
    assert abs((after["n"] - after["last_seen_at"]).total_seconds()) < 5
    assert 29.9 < (after["expires_at"] - after["n"]).total_seconds() / 86400 <= 30.0
    assert (await web.get("/api/me", headers=h)).status_code == 200  # right after: no second write
    assert (await web.db.fetchrow("SELECT last_seen_at FROM web_sessions"))["last_seen_at"] == after["last_seen_at"]
    # GET /session reports the renewed expiry
    assert (await web.get("/api/auth/web/session", headers=h)).json()["expiresAt"]


# ---------------------------------------------------------------- test-user rule

async def test_bot_users_are_not_test_users_but_dev_rules_apply(web):
    await login_code(web, TG, first_name="e2e-looking")  # DEV_AUTH is on in tests: the dev rule (name) applies
    assert await web.db.fetchval("SELECT is_test FROM users WHERE tg_id = $1", TG) is True
    await login_code(web, TG2, first_name="Real Person")
    assert await web.db.fetchval("SELECT is_test FROM users WHERE tg_id = $1", TG2) is False
    await login_code(web, 10**12 + 5)  # the reserved range
    assert await web.db.fetchval("SELECT is_test FROM users WHERE tg_id = $1", 10**12 + 5) is True


async def test_without_dev_auth_a_bot_user_is_never_a_test_user(make_client, clean_db, clean_redis, fake_sim):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                          rate_limit_enabled=False, dev_auth=False, bot_username=BOT)
    r = await c.post("/internal/auth/login-code", headers=TOKEN_H, json={"tg_id": 10**12 + 7, "first_name": "e2e"})
    assert r.status_code == 200
    assert await clean_db.fetchval("SELECT is_test FROM users WHERE tg_id = $1", 10**12 + 7) is False


# ---------------------------------------------------------------- housekeeping

async def test_old_dead_rows_are_purged_on_the_write_paths(web):
    await code_login(web, TG)
    await web.db.execute("UPDATE web_sessions SET expires_at = now() - interval '8 days'")
    await web.db.execute("UPDATE web_login_codes SET expires_at = now() - interval '8 days'")
    await login_code(web, TG2)
    assert await web.db.fetchval("SELECT count(*) FROM web_sessions") == 0
    assert await web.db.fetchval("SELECT count(*) FROM web_login_codes") == 1
