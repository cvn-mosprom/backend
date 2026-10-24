"""S10 raw transport test: what a BROWSER needs from the wire, checked without any client library.

http-contract.md "Общее": the web client (origin of the Vite dev server) sends `X-Telegram-Init-Data` / `X-Dev-User` / `Content-Type` and reads `Retry-After`;
a browser first sends a preflight, and hides every answer without CORS headers behind a bare "network error" - so 4xx / 429 / 5xx need them too.
Env: E2E_BASE (conftest), E2E_ORIGIN (default http://localhost:5173 = the api's dev CORS origin), BOT_TOKEN / E2E_BOT_TOKEN or the repo `.env`
(signed initData; that part is skipped when the stack has no token), E2E_DOCKER=1 (5xx: stops the sim container, always starts it again).
"""

import hashlib
import hmac
import json
import os
import random
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from conftest import BASE
from smoke.stack import DOCKER, compose, wait_ready, wait_sim_healthy

ORIGIN = os.environ.get("E2E_ORIGIN", "http://localhost:5173")
ROOT = Path(__file__).resolve().parents[1]
SENT_HEADERS = ["content-type", "x-dev-user", "x-telegram-init-data"]
ME = "/api/factory/me"  # failed auth is rate limited per IP like everything else: the 60/min bucket of this route fits the cases below


def new_id() -> str:
    return str(random.randint(10**9, 10**10 - 1))


class PatientClient(httpx.Client):
    """Waits out a 429 (the per-IP bucket of failed auth may be warm from another suite) - except in the 429 test, which uses the plain `raw` client."""

    def request(self, *args, **kw):
        for _ in range(3):
            r = super().request(*args, **kw)
            wait = r.headers.get("retry-after", "")
            if r.status_code != 429 or not wait.isdigit() or int(wait) > 60:
                break
            time.sleep(int(wait) + 1)
        return r


@pytest.fixture(scope="module")
def http():
    with PatientClient(base_url=BASE, verify=False, timeout=30.0) as c:
        yield c


@pytest.fixture(scope="module")
def raw():
    with httpx.Client(base_url=BASE, verify=False, timeout=30.0) as c:
        yield c


def dev(user: str | None = None, **extra: str) -> dict[str, str]:
    return {"Origin": ORIGIN, "X-Dev-User": user or new_id(), **extra}


def cors(r: httpx.Response) -> None:
    """The headers without which the browser throws the response away."""
    assert r.headers.get("access-control-allow-origin") == ORIGIN, dict(r.headers)
    assert "retry-after" in r.headers.get("access-control-expose-headers", "").lower()
    assert "origin" in r.headers.get("vary", "").lower()


def error_body(r: httpx.Response, code: str | None = None) -> dict:
    assert r.headers["content-type"].startswith("application/json"), r.text[:200]
    e = r.json()["error"]
    assert set(e) >= {"code", "message", "details"}, e
    if code:
        assert e["code"] == code, e
    return e


# ------------------------------------------------------------------ preflight
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/bootstrap"), ("POST", "/api/command"), ("POST", "/api/advance"), ("GET", "/api/factory/me"),
    ("GET", "/api/season/leaderboard"), ("GET", "/api/season/summary"), ("POST", "/api/leads"), ("GET", "/api/leads/me"),
    ("DELETE", "/api/leads/me"), ("POST", "/api/quiz/answer"), ("POST", "/api/events"),
])
def test_preflight_is_answered_without_auth(http, method, path):
    r = http.request("OPTIONS", path, headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": ", ".join(SENT_HEADERS)})
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert method in [m.strip() for m in r.headers["access-control-allow-methods"].split(",")]
    allowed = [h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")]
    for h in SENT_HEADERS:
        assert h in allowed, (h, allowed)
    assert int(r.headers["access-control-max-age"]) > 0


def test_preflights_are_never_rate_limited(http):
    hdr = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    assert {http.request("OPTIONS", "/api/command", headers=hdr).status_code for _ in range(60)} == {200}


def test_a_foreign_origin_gets_no_cors_headers(http):
    evil = "https://evil.example"
    pre = http.request("OPTIONS", "/api/command", headers={"Origin": evil, "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in pre.headers
    r = http.get("/api/season/summary", headers={"Origin": evil, "X-Dev-User": new_id()})
    assert "access-control-allow-origin" not in r.headers


# ------------------------------------------------------------------ CORS on answers
def test_cors_on_200(http):
    r = http.get("/api/bootstrap", headers=dev())
    assert r.status_code == 200
    cors(r)
    assert r.headers.get("x-request-id")
    assert "x-request-id" in r.headers["access-control-expose-headers"].lower()  # the client may quote it in a bug report


def test_cors_on_4xx(http):
    user = new_id()
    http.get("/api/bootstrap", headers=dev(user))  # the 409 needs a factory
    cases = {
        401: http.get("/api/bootstrap", headers={"Origin": ORIGIN}),  # no credentials at all
        404: http.get("/api/leads/me", headers=dev(user)),  # no lead yet
        "404 path": http.get("/api/definitely-not-here", headers=dev(user)),
        # only a command with effect can conflict (buildEnd is a no-op)
        409: http.post("/api/command", headers=dev(user), json={
            "version": 41, "idempotency_key": "s10-transport-409", "cmd": {"type": "renameFactory", "name": "x"}}),
        422: http.post("/api/command", headers=dev(user), content=b"{not json"),
    }
    for key, r in cases.items():
        want = key if isinstance(key, int) else 404
        assert r.status_code == want, (key, r.status_code, r.text[:200])
        cors(r)
        error_body(r)
    assert error_body(cases[409])["code"] == "version_conflict"
    assert isinstance(error_body(cases[409])["details"]["version"], int)  # the client resyncs from details.version
    assert error_body(cases[401])["code"] == "unauthorized"
    assert error_body(cases[422])["code"] == "validation_error"


def test_cors_and_retry_after_on_429(raw):
    user = new_id()
    h = dev(user)
    raw.get("/api/bootstrap", headers=h)
    last = None
    for _ in range(14):  # /api/advance: 10 a minute per player
        last = raw.post("/api/advance", headers=h, json={"cap_hours": 8, "reason": "heartbeat"})
        if last.status_code == 429:
            break
    assert last is not None and last.status_code == 429, last.status_code
    cors(last)
    assert error_body(last, "rate_limited")
    assert 0 < int(last.headers["retry-after"]) <= 60  # the client sleeps this long (<= 10 s) and retries once


@DOCKER
def test_cors_on_5xx_with_the_sim_down(http):
    """503 sim_unavailable: built by a layer below CORS; the browser must still be able to read it."""
    try:
        compose("stop", "sim")
        r = http.get("/api/bootstrap", headers=dev())  # a new player needs the sim
        assert r.status_code == 503, (r.status_code, r.text[:200])
        cors(r)
        error_body(r, "sim_unavailable")
    finally:
        compose("start", "sim")
        wait_sim_healthy()
        wait_ready()


# ------------------------------------------------------------------ auth headers
def player(r: httpx.Response) -> dict:
    assert r.status_code == 200, r.text[:300]
    return r.json()["snapshot"]["player"]


def test_x_dev_user_authenticates_and_is_the_same_player_as_authorization_dev(http):
    uid = new_id()
    a = player(http.get("/api/bootstrap", headers={"X-Dev-User": uid}))
    b = player(http.get("/api/bootstrap", headers={"Authorization": f"dev {uid}"}))
    assert a["tgId"] == int(uid) and b["tgId"] == int(uid)
    assert a["id"] == b["id"]  # one person, two spellings of the same credential
    assert player(http.get("/api/bootstrap", headers={"X-Dev-User": new_id()}))["id"] != a["id"]


@pytest.mark.parametrize("bad", ["", "abc", "-5", "0", "1.5"])
def test_x_dev_user_garbage_is_401(http, bad):
    r = http.get(ME, headers={"Origin": ORIGIN, "X-Dev-User": bad})
    assert r.status_code == 401, (bad, r.status_code, r.text[:200])
    cors(r)
    error_body(r)


def test_empty_x_telegram_init_data_is_401_and_is_not_rescued_by_a_dev_header(http):
    """The client sends X-Telegram-Init-Data only when it has initData; an empty one is a broken session, not a dev user."""
    for headers in ({"X-Telegram-Init-Data": ""}, {"X-Telegram-Init-Data": "", "X-Dev-User": new_id()}):
        r = http.get(ME, headers={"Origin": ORIGIN, **headers})
        assert r.status_code == 401, (headers, r.status_code, r.text[:200])
        cors(r)
        error_body(r, "unauthorized")


def test_garbage_init_data_is_refused(http):
    r = http.get(ME, headers={"Origin": ORIGIN, "X-Telegram-Init-Data": "query_id=x&user=%7B%7D&auth_date=1&hash=00"})
    assert r.status_code in (401, 503), r.status_code  # 401 invalid_init_data; 503 config_error when the stack has no BOT_TOKEN
    cors(r)
    assert error_body(r)["code"] in ("invalid_init_data", "config_error", "auth_expired")


def bot_token() -> str:
    token = os.environ.get("E2E_BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
    env = ROOT / ".env"
    if not token and env.exists():
        m = re.search(r"^BOT_TOKEN=(.*)$", env.read_text(encoding="utf-8"), re.M)
        token = m.group(1).strip().strip("\"'") if m else ""
    return token


def signed_init_data(token: str, tg_id: int, start_param: str | None = None, age_s: int = 5) -> str:
    """initData as Telegram builds it (HMAC-SHA256 of the sorted key=value lines, key = HMAC("WebAppData", bot token))."""
    fields = {"auth_date": str(int(time.time()) - age_s), "query_id": "AAE2E2E", "user": json.dumps({"id": tg_id, "first_name": "E2E"}, separators=(",", ":"))}
    if start_param:
        fields["start_param"] = start_param
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def test_signed_init_data(http):
    token = bot_token()
    if not token:
        pytest.skip("the stack has no BOT_TOKEN (.env / E2E_BOT_TOKEN): signed X-Telegram-Init-Data cannot be checked")
    uid = int(new_id())
    ok = http.get("/api/bootstrap", headers={"Origin": ORIGIN, "X-Telegram-Init-Data": signed_init_data(token, uid, "ref_s10")})
    assert player(ok)["tgId"] == uid
    cors(ok)
    tampered = signed_init_data(token, uid).replace("E2E", "EVIL")
    assert http.get(ME, headers={"X-Telegram-Init-Data": tampered}).status_code == 401
    assert error_body(http.get(ME, headers={"X-Telegram-Init-Data": signed_init_data(token, uid, age_s=10 * 86400)}), "auth_expired")
    # the same player through the Authorization spelling
    again = http.get("/api/bootstrap", headers={"Authorization": "tma " + signed_init_data(token, uid)})
    assert player(again)["id"] == player(ok)["id"]
