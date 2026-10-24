"""S8: transport between the web client (vendor/frontend HttpGameApi) and the api.

Auth headers (X-Telegram-Init-Data / X-Dev-User), CORS (outermost, preflight short-circuit), advance reasons and
limit, 409 details.version, bootstrap ?start_param= fallback, features, top-level toast / rewards / snapshot."""
from __future__ import annotations

import pytest

from app.ratelimit import LIMITS
from app.settings import Settings
from app.testing import make_init_data
from tests.game_helpers import advance, bootstrap, command, key, me, place, shift
from tests.helpers import BOT_TOKEN, TEST_DB_URL, TEST_REDIS_URL, dev, tma
from tests.test_errors import assert_error

ORIGIN = "http://localhost:5173"
ACAO = "access-control-allow-origin"
GOOD_USER = {"id": 77, "first_name": "Ann", "username": "ann"}


def init(user: dict | None = None, **kw) -> dict[str, str]:
    return {"X-Telegram-Init-Data": make_init_data(BOT_TOKEN, user or GOOD_USER, **kw)}


async def whoami(c, headers: dict[str, str]):
    return await c.get("/api/me", headers=headers)


# ================================================================ auth headers

async def test_x_telegram_init_data_authenticates_like_tma(live):
    r = await whoami(live, init())
    assert r.status_code == 200, r.text
    assert r.json()["tgId"] == 77 and r.json()["name"] == "Ann" and r.json()["username"] == "ann"
    assert (await whoami(live, tma(GOOD_USER))).json()["id"] == r.json()["id"]  # same user through Authorization: tma


async def test_x_dev_user_authenticates_with_the_dev_name(live):
    r = await whoami(live, {"X-Dev-User": "123456789"})
    assert r.status_code == 200, r.text
    assert r.json()["tgId"] == 123456789 and r.json()["name"] == "Dev123456789"  # the client sends no ":<name>"
    assert (await whoami(live, dev(123456789))).json()["id"] == r.json()["id"]  # same user as Authorization: dev


async def test_authorization_has_priority_over_both_headers(live):
    both = {**init({"id": 88, "first_name": "Zed"}), "X-Dev-User": "6"}
    assert (await whoami(live, {**dev(5), **both})).json()["tgId"] == 5
    assert (await whoami(live, {**tma(GOOD_USER), **both})).json()["tgId"] == 77


async def test_init_data_header_has_priority_over_x_dev_user(live):
    r = await whoami(live, {**init(), "X-Dev-User": "6"})
    assert r.json()["tgId"] == 77


async def test_a_present_header_is_never_rescued_by_a_lower_one(make_client):
    c = await make_client()
    # unusable Authorization + valid dev header
    assert_error(await whoami(c, {"Authorization": "Bearer abc", "X-Dev-User": "6"}), 401, "unauthorized")
    # broken initData + dev header: invalid_init_data, not a silent dev login
    bad = {"X-Telegram-Init-Data": "auth_date=1&hash=0", "X-Dev-User": "6"}
    assert_error(await whoami(c, bad), 401, "invalid_init_data")


@pytest.mark.parametrize("value", ["", "   "])
async def test_empty_x_telegram_init_data_is_401_unauthorized(make_client, value):
    c = await make_client()
    assert_error(await whoami(c, {"X-Telegram-Init-Data": value}), 401, "unauthorized")
    assert_error(await whoami(c, {"X-Telegram-Init-Data": value, "X-Dev-User": "6"}), 401, "unauthorized")


async def test_x_telegram_init_data_errors_match_the_tma_ones(make_client):
    c = await make_client()
    tampered = make_init_data(BOT_TOKEN, GOOD_USER).replace("Ann", "Mallory")
    assert_error(await whoami(c, {"X-Telegram-Init-Data": tampered}), 401, "invalid_init_data")
    old = init(auth_date=1)
    assert_error(await whoami(c, old), 401, "auth_expired")


@pytest.mark.parametrize("settings", [{"app_env": "prod", "dev_auth": True}, {"dev_auth": False}])
async def test_x_dev_user_is_refused_in_prod_and_without_dev_auth(make_client, settings):
    c = await make_client(**settings)
    assert_error(await whoami(c, {"X-Dev-User": "5"}), 401, "unauthorized")
    assert_error(await whoami(c, {"X-Dev-User": "5", **dev(5)}), 401, "unauthorized")


@pytest.mark.parametrize("value", ["abc", "0", "-4", "", "9223372036854775808", "1.5"])
async def test_x_dev_user_malformed(make_client, value):
    c = await make_client()
    assert_error(await whoami(c, {"X-Dev-User": value}), 401, "invalid_init_data")


async def test_prod_still_takes_init_data_in_both_headers(make_client, clean_db):
    c = await make_client(app_env="prod", dev_auth=True, database_url=TEST_DB_URL)
    assert (await whoami(c, init())).status_code == 200
    assert (await whoami(c, tma(GOOD_USER))).status_code == 200


# ================================================================ CORS: settings

def test_cors_origins_default_and_derivation():
    assert Settings(app_env="dev").cors_origin_list == ["http://localhost:5173"]
    assert Settings(app_env="test").cors_origin_list == ["http://localhost:5173"]
    explicit = Settings(app_env="dev", cors_origins=" https://a.example/, http://b.example:8080 ,,")
    assert explicit.cors_origin_list == ["https://a.example", "http://b.example:8080"]
    prod = Settings(app_env="prod", webapp_url="https://Game.Example.com/app/?x=1")
    assert prod.cors_origin_list == ["https://game.example.com"]  # scheme://host of WEBAPP_URL, no path
    assert Settings(app_env="prod", domain="game.example.com").cors_origin_list == ["https://game.example.com"]
    assert Settings(app_env="prod", domain="http://localhost").cors_origin_list == ["http://localhost"]
    assert Settings(app_env="prod").cors_origin_list == []  # nothing to derive: no cross-origin access at all
    assert Settings(app_env="prod", webapp_url="https://w.example", cors_origins="https://o.example").cors_origin_list == [
        "https://o.example"]  # CORS_ORIGINS wins over WEBAPP_URL


# ================================================================ CORS: preflight

PREFLIGHT = {"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "x-telegram-init-data,x-dev-user,content-type"}


async def test_preflight_answers_without_auth_and_lists_the_contract(make_client):
    c = await make_client()
    for path in ("/api/command", "/api/bootstrap", "/api/advance", "/api/leads/me"):
        r = await c.options(path, headers=PREFLIGHT)  # no credentials at all
        assert r.status_code == 200, (path, r.text)
        assert r.headers[ACAO] == ORIGIN
        allowed = {h.strip().lower() for h in r.headers["access-control-allow-headers"].split(",")}
        assert {"x-telegram-init-data", "x-dev-user", "content-type", "authorization"} <= allowed
        assert {m.strip() for m in r.headers["access-control-allow-methods"].split(",")} >= {"GET", "POST", "DELETE"}
        assert r.headers["access-control-max-age"] == "600"


async def test_preflight_for_delete(make_client):
    c = await make_client()
    r = await c.options("/api/leads/me", headers={**PREFLIGHT, "Access-Control-Request-Method": "DELETE"})
    assert r.status_code == 200 and r.headers[ACAO] == ORIGIN


async def test_preflight_from_a_foreign_origin_is_refused(make_client):
    c = await make_client()
    r = await c.options("/api/command", headers={**PREFLIGHT, "Origin": "https://evil.example"})
    assert r.status_code == 400 and ACAO not in r.headers
    r = await c.options("/api/command", headers={**PREFLIGHT, "Access-Control-Request-Headers": "x-evil"})
    assert r.status_code == 400
    r = await c.options("/api/command", headers={**PREFLIGHT, "Access-Control-Request-Method": "PUT"})
    assert r.status_code == 400


async def test_preflight_skips_the_rate_limiter(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 1, "command": 1})
    for _ in range(5):
        assert (await c.options("/api/me", headers=PREFLIGHT)).status_code == 200
        assert (await c.options("/api/command", headers=PREFLIGHT)).status_code == 200
    assert not [k async for k in clean_redis.scan_iter("rl:*")]  # not even counted
    assert (await c.get("/t/limited", headers=dev(1))).status_code == 200  # the budget is untouched


async def test_preflight_skips_the_request_timeout(make_client):
    c = await make_client(request_timeout_s=0.05)
    r = await c.options("/t/sleep/1", headers=PREFLIGHT)
    assert r.status_code == 200 and r.headers[ACAO] == ORIGIN


async def test_origin_list_is_configurable(make_client):
    c = await make_client(cors_origins="https://one.example, https://two.example")
    for origin in ("https://one.example", "https://two.example"):
        r = await c.options("/api/command", headers={**PREFLIGHT, "Origin": origin})
        assert r.status_code == 200 and r.headers[ACAO] == origin
    assert (await c.options("/api/command", headers=PREFLIGHT)).status_code == 400  # the dev default is replaced
    prod = await make_client(app_env="prod", webapp_url="https://app.example/play")
    assert (await prod.options("/api/command", headers={**PREFLIGHT, "Origin": "https://app.example"})).status_code == 200
    assert (await prod.options("/api/command", headers=PREFLIGHT)).status_code == 400  # prod: no localhost


# ================================================================ CORS: headers on every kind of response

def assert_cors(resp, origin: str = ORIGIN) -> None:
    assert resp.headers.get(ACAO) == origin, dict(resp.headers)
    exposed = {h.strip().lower() for h in resp.headers["access-control-expose-headers"].split(",")}
    assert {"retry-after", "x-request-id"} <= exposed
    assert "origin" in resp.headers["vary"].lower()


async def test_cors_headers_on_200_401_422_500(make_client):
    c = await make_client()
    h = {"Origin": ORIGIN}
    ok = await c.get("/t/small", headers=h)
    assert ok.status_code == 200
    assert_cors(ok)
    unauth = await c.get("/api/me", headers=h)
    assert unauth.status_code == 401
    assert_cors(unauth)
    invalid = await c.post("/t/echo", json={"name": 5}, headers=h)
    assert invalid.status_code == 422
    assert_cors(invalid)
    boom = await c.get("/t/boom", headers=h)
    assert boom.status_code == 500 and boom.json()["error"]["code"] == "internal_error"
    assert_cors(boom)


async def test_cors_headers_on_404_and_405(make_client):
    c = await make_client()
    assert_cors(await c.get("/api/nope", headers={"Origin": ORIGIN}))
    assert_cors(await c.put("/t/small", headers={"Origin": ORIGIN}))


async def test_cors_headers_on_the_request_timeout_503(make_client):
    c = await make_client(request_timeout_s=0.1)
    r = await c.get("/t/sleep/1", headers={"Origin": ORIGIN})
    assert r.status_code == 503 and r.json()["error"]["code"] == "timeout" and r.headers["retry-after"] == "1"
    assert_cors(r)


async def test_cors_headers_on_429_with_retry_after_in_both_places(make_client, clean_redis):
    c = await make_client(redis_url=TEST_REDIS_URL, rate_limit_overrides={"default": 1})
    h = {"Origin": ORIGIN, **dev(1)}
    assert (await c.get("/t/limited", headers=h)).status_code == 200
    r = await c.get("/t/limited", headers=h)
    err = assert_error(r, 429, "rate_limited")
    assert_cors(r)
    assert int(r.headers["retry-after"]) >= 1
    assert err["details"]["retry_after"] == int(r.headers["retry-after"])  # contract: header or details.retry_after


async def test_429_without_a_retry_after_header_gets_both(make_client):
    c = await make_client()
    err = assert_error(await c.get("/t/apperror/rate_limited"), 429, "rate_limited")
    assert err["details"]["retry_after"] == 1 and err["details"]["x"] == 1


async def test_cors_headers_on_game_errors_422_and_409(game):
    h = {"Origin": ORIGIN, **dev(1)}
    await bootstrap(game)
    ok = await game.post("/api/command", json={"version": 0, "idempotency_key": key(1), "cmd": place()}, headers=h)
    assert ok.status_code == 200
    assert_cors(ok)
    conflict = await game.post("/api/command", json={"version": 0, "idempotency_key": key(2), "cmd": place(2, 2)}, headers=h)
    assert_error(conflict, 409, "version_conflict")
    assert_cors(conflict)
    rejected = await game.post("/api/command", json={"version": 1, "idempotency_key": key(3), "cmd": {"type": "undo"}},
                               headers=h)
    assert rejected.status_code == 422
    assert_cors(rejected)


async def test_foreign_origin_gets_no_cors_headers(make_client):
    c = await make_client()
    r = await c.get("/t/small", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200 and ACAO not in r.headers
    assert ACAO not in (await c.get("/t/small")).headers  # same-origin / curl: nothing added


# ================================================================ advance

@pytest.mark.parametrize("reason", ["heartbeat", "order_due"])
async def test_advance_accepts_the_web_clients_reasons_and_sends_manual_to_the_sim(game, fake_sim, reason):
    await bootstrap(game)
    r = await advance(game, {"cap_hours": 8, "reason": reason})
    assert r.status_code == 200, r.text
    assert fake_sim.bodies("/v1/advance")[-1]["reason"] == "manual"  # the sim only knows resume | manual | background


async def test_advance_still_rejects_unknown_reasons(game):
    await bootstrap(game)
    assert_error(await advance(game, {"reason": "whenever"}), 422, "validation_error")


def test_advance_limit_is_not_below_ten_a_minute():
    assert LIMITS["advance"].max_requests >= 10 and LIMITS["advance"].window_s == 60  # the client keeps <= 8/min


async def test_ten_advances_a_minute_are_served_and_flooding_gets_a_429(make_client, clean_db, clean_redis, fake_sim):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL)
    await bootstrap(c)
    for i in range(10):
        r = await advance(c, {"reason": "heartbeat" if i % 2 else "order_due"})
        assert r.status_code == 200, (i, r.text)
    for _ in range(LIMITS["advance"].max_requests):
        r = await advance(c, {"reason": "heartbeat"})
        if r.status_code == 429:
            break
    assert_error(r, 429, "rate_limited")


# ================================================================ 409 details.version

async def test_version_conflict_carries_details_version(game):
    await bootstrap(game)
    assert (await command(game, 0, key(1), place())).status_code == 200
    err = assert_error(await command(game, 0, key(2), place(2, 2)), 409, "version_conflict")
    assert err["details"]["version"] == 1 == err["details"]["current_version"]


# ================================================================ bootstrap ?start_param=

async def source_of(game, tg_id: int) -> str | None:
    return await game.db.fetchval("SELECT source FROM users WHERE tg_id = $1", tg_id)


async def boot(game, headers: dict[str, str], start_param: str | None = None):
    params = {"start_param": start_param} if start_param is not None else None
    r = await game.get("/api/bootstrap", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r


async def test_start_param_query_fills_an_empty_source(game):
    await boot(game, dev(1), "ref_7")
    assert await source_of(game, 1) == "ref_7"


async def test_start_param_query_is_written_once(game):
    await boot(game, dev(1))  # first visit without a source
    assert await source_of(game, 1) is None
    await boot(game, dev(1), "src_a")  # a later visit fills the empty source ...
    assert await source_of(game, 1) == "src_a"
    await boot(game, dev(1), "src_b")  # ... and never overwrites it
    assert await source_of(game, 1) == "src_a"


async def test_start_param_of_init_data_wins_over_the_query(game):
    await boot(game, init(start_param="from_tg"), "from_query")
    assert await source_of(game, 77) == "from_tg"


async def test_start_param_query_is_used_when_init_data_has_none(game):
    await boot(game, init({"id": 78, "first_name": "Bo"}), "ref_9")
    assert await source_of(game, 78) == "ref_9"


async def test_start_param_query_is_trimmed(game):
    await boot(game, dev(2), "  ref_1  ")
    assert await source_of(game, 2) == "ref_1"


@pytest.mark.parametrize("value", ["", "   ", "x" * 129, "a\nb", "a\x00b"])
async def test_unusable_start_param_is_ignored_not_an_error(game, value):
    r = await boot(game, dev(3), value)
    assert r.json()["snapshot"] and await source_of(game, 3) is None


# ================================================================ features

async def test_bootstrap_features_include_quiz_and_leads(game):
    feats = (await bootstrap(game))["features"]
    assert feats["quiz"] is True and feats["leads"] is True
    assert "undo" in feats and "serverOrderEta" in feats  # unchanged flags stay


# ================================================================ top-level toast / rewards / snapshot

async def test_command_response_has_top_level_toast_and_rewards(game):
    await bootstrap(game)
    placed = (await command(game, 0, key(1), place())).json()
    assert placed["toast"] == placed["result"]["toast"] and "rewards" not in placed
    shifted = (await command(game, 1, key(2), shift())).json()
    assert shifted["rewards"] == shifted["result"]["rewards"] == {"xp": 10}
    assert shifted["toast"] == shifted["result"]["toast"]


async def test_replay_keeps_top_level_toast_and_rewards(game):
    await bootstrap(game)
    first = (await command(game, 0, key(1), shift())).json()
    again = (await command(game, 99, key(1), shift())).json()  # replay is checked before the version
    assert again["replayed"] is True
    assert again["toast"] == first["toast"] and again["rewards"] == first["rewards"]


def test_lift_short_copies_only_what_exists():
    from app.routes.game import lift_short
    assert lift_short({"result": {}}) == {"result": {}}
    assert lift_short({"result": {"toast": "t"}}) == {"result": {"toast": "t"}, "toast": "t"}
    assert lift_short({"result": {"rewards": {"xp": 1}, "toast": None}}) == {
        "result": {"rewards": {"xp": 1}, "toast": None}, "rewards": {"xp": 1}}
    assert lift_short({"result": None, "snapshot": None}) == {"result": None, "snapshot": None}  # buildEnd shape


async def test_rejection_carries_a_top_level_snapshot_next_to_error(game):
    await bootstrap(game)
    r = await command(game, 0, key(1), {"type": "undo"})
    body = r.json()
    assert r.status_code == 422 and body["error"]["code"] == "command_rejected"
    assert body["error"]["details"]["reason"] == "history_unavailable"
    snap = body["snapshot"]
    assert snap["factory"] and snap["player"]  # a GameSnapshot the client can apply
    assert snap["version"] == 0 == body["error"]["details"]["version"]  # the client takes the version from it
    assert body["error"]["details"]["snapshot"]["factory"] == snap["factory"]  # details.snapshot is kept


async def test_rule_rejection_of_a_shift_has_a_top_level_snapshot_too(game):
    await bootstrap(game)
    assert (await command(game, 0, key(1), place())).status_code == 200
    r = await command(game, 1, key(2), shift(durationMs=10))
    body = r.json()
    assert r.status_code == 422 and body["error"]["code"] == "minigame_too_short"
    assert body["snapshot"]["version"] == 1  # a rejection does not bump the version


async def test_replayed_rejection_has_the_snapshot_as_well(game):
    await bootstrap(game)
    await command(game, 0, key(1), {"type": "undo"})
    r = await command(game, 0, key(1), {"type": "undo"})
    assert r.status_code == 422 and r.json()["error"]["details"]["replayed"] is True and r.json()["snapshot"]["factory"]


async def test_factory_me_after_a_conflict_is_what_the_client_resyncs_with(game):
    """409 -> the client reads details.version and calls GET /api/factory/me (no 409 there)."""
    await bootstrap(game)
    await command(game, 0, key(1), place())
    err = assert_error(await command(game, 0, key(2), place(2, 2)), 409, "version_conflict")
    body = (await me(game)).json()
    assert body["version"] == err["details"]["version"] and body["snapshot"]["factory"]
