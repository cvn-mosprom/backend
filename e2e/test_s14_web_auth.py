"""S14 against the running stack: web login through the Telegram bot, over Caddy. The bot side is played by
`docker compose exec api` (the token-protected /internal/auth/* endpoints are not reachable from outside: Caddy answers 404),
the web side talks to the public /api/auth/web/* endpoints with `Authorization: Bearer <token>`.

The public auth endpoints are limited to 5 calls/min per IP, so the helpers wait out a 429; the wrong-code lockout (5 wrong
codes lock the IP for 10 min) is never approached: at most one wrong code is sent per test run, and a success clears it.
"""

from __future__ import annotations

import random
import re
import time

import httpx
import pytest
from conftest import BASE, err
from test_s4_rotation import internal

NAME = "e2e-web"  # an "e2e" name = test user: kept out of the leaderboard and the analytics


@pytest.fixture(scope="module")
def web() -> httpx.Client:
    with httpx.Client(base_url=BASE, verify=False, timeout=30.0) as c:
        yield c


def tg_id() -> int:
    return random.randint(10**9, 10**10 - 1)


def post(web: httpx.Client, path: str, **kw) -> httpx.Response:
    for _ in range(4):
        r = web.post(path, **kw)
        if r.status_code != 429:
            return r
        time.sleep(min(float(r.headers.get("Retry-After", "5")) + 1, 65))
    return r


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def issue_code(tg: int) -> str:
    status, body = internal("/internal/auth/login-code", body={"tg_id": tg, "first_name": NAME, "username": "e2eweb"})
    assert status == 200, body
    assert re.fullmatch(r"\d{6}", body["code"]) and body["expiresAt"]
    return body["code"]


def login(web: httpx.Client, tg: int) -> dict:
    r = post(web, "/api/auth/web/code", json={"code": issue_code(tg)})
    assert r.status_code == 200, r.text
    return r.json()


def test_internal_auth_is_not_reachable_through_caddy(web):
    r = web.post("/internal/auth/login-code", json={"tg_id": 1}, headers={"X-Internal-Token": "x"})
    assert r.status_code == 404


@pytest.mark.parametrize("path, body", [
    ("/internal/auth/login-code", {"tg_id": 1}),
    ("/internal/auth/approve", {"nonce": "x", "tg_id": 1}),
    ("/internal/auth/revoke-all", {"tg_id": 1}),
])
def test_internal_endpoints_need_the_token(path, body):
    assert internal(path, token="", body=body)[0] == 401
    assert internal(path, token="not-the-token", body=body)[0] == 401


def test_code_login_session_and_logout(web):
    tg = tg_id()
    s = login(web, tg)
    assert s["user"]["tgId"] == tg and s["token"] and s["expiresAt"]
    h = bearer(s["token"])

    g = web.get("/api/auth/web/session", headers=h)
    assert g.status_code == 200 and g.json()["user"]["tgId"] == tg
    boot = web.get("/api/bootstrap", headers=h)
    assert boot.status_code == 200 and boot.json()["snapshot"]["player"]["tgId"] == tg

    out = web.delete("/api/auth/web/session", headers=h)
    assert out.status_code == 204
    assert err(web.get("/api/bootstrap", headers=h))["code"] == "unauthorized"


def test_code_is_single_use_and_a_wrong_code_is_rejected(web):
    tg = tg_id()
    code = issue_code(tg)
    assert post(web, "/api/auth/web/code", json={"code": code}).status_code == 200
    assert err(post(web, "/api/auth/web/code", json={"code": code}))["code"] == "invalid_code"  # reused


def test_web_and_telegram_side_are_one_user_and_one_factory(web):
    tg = tg_id()
    s = login(web, tg)
    # the "Telegram side" (dev auth here) opens the game first, the web side then sees the very same factory
    dev = httpx.Client(base_url=BASE, verify=False, timeout=30.0, headers={"Authorization": f"dev {tg}:{NAME}"})
    try:
        d = dev.get("/api/bootstrap")
        assert d.status_code == 200
        w = web.get("/api/bootstrap", headers=bearer(s["token"]))
        assert w.status_code == 200
        assert w.json()["version"] == d.json()["version"]
        assert w.json()["snapshot"]["player"]["id"] == d.json()["snapshot"]["player"]["id"]
        # a command through the web session moves the factory the Telegram side sees
        cmd = {"version": w.json()["version"], "idempotency_key": f"s14-{tg}", "cmd": {"type": "renameFactory", "name": "Web Fabrika"}}
        c = web.post("/api/command", json=cmd, headers=bearer(s["token"]))
        assert c.status_code == 200, c.text
        assert dev.get("/api/factory/me").json()["version"] == c.json()["version"]
    finally:
        dev.close()


def test_revoke_all_kills_the_sessions(web):
    tg = tg_id()
    s = login(web, tg)
    assert web.get("/api/me", headers=bearer(s["token"])).status_code == 200
    status, body = internal("/internal/auth/revoke-all", body={"tg_id": tg})
    assert (status, body) == (200, {"revoked": 1})
    assert err(web.get("/api/me", headers=bearer(s["token"])))["code"] == "unauthorized"


def test_deep_link_login(web):
    r = post(web, "/api/auth/web/start")
    if r.status_code == 503:
        assert r.json()["error"]["code"] == "config_error"
        pytest.skip("BOT_USERNAME is not set for the api: /start answers 503 by design")
    assert r.status_code == 200, r.text
    b = r.json()
    assert re.fullmatch(r"https://t\.me/\w+\?start=weblogin_" + re.escape(b["nonce"]), b["botUrl"])
    p = post(web, "/api/auth/web/poll", json={"nonce": b["nonce"]})
    assert p.status_code == 202 and p.json() == {"status": "pending"}
    tg = tg_id()
    status, body = internal("/internal/auth/approve", body={"nonce": b["nonce"], "tg_id": tg, "first_name": NAME})
    assert (status, body) == (200, {"ok": True})
    p = post(web, "/api/auth/web/poll", json={"nonce": b["nonce"]})
    assert p.status_code == 200 and p.json()["user"]["tgId"] == tg
    assert web.get("/api/me", headers=bearer(p.json()["token"])).status_code == 200
    assert err(post(web, "/api/auth/web/poll", json={"nonce": b["nonce"]}))["code"] == "login_expired"
