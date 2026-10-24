"""/health and /ready."""
from __future__ import annotations

import time

import httpx
import pytest

from app.routes import health as health_routes
from tests.helpers import TEST_DB_URL, TEST_REDIS_URL, build_settings, mock_sim
from tests.test_errors import assert_error


async def ok(*_a, **_k) -> str:
    return "ok"


async def bad(*_a, **_k) -> str:
    raise ConnectionError("boom")


@pytest.fixture
def db_ok(monkeypatch):
    monkeypatch.setattr(health_routes, "check_db", ok)
    monkeypatch.setattr(health_routes, "check_migrations", ok)


@pytest.fixture
def redis_ok(monkeypatch):
    monkeypatch.setattr(health_routes, "check_redis", ok)


def sim_with(status: int | None = None, exc: Exception | None = None, body: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if exc:
            raise exc
        return httpx.Response(status, json=body or {})

    return mock_sim(handler)


async def test_health(make_client):
    c = await make_client()
    before = int(time.time() * 1000)
    resp = await c.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["service"] == "api"
    assert before - 1000 <= body["serverTime"] <= int(time.time() * 1000) + 1000
    assert resp.headers["x-request-id"]


async def test_health_needs_no_auth_or_dependencies(make_client):
    c = await make_client(database_url="", redis_url="")  # nothing configured at all
    assert (await c.get("/health")).status_code == 200


async def test_ready_db_unavailable(make_client, redis_ok, monkeypatch):
    monkeypatch.setattr(health_routes, "check_db", bad)
    monkeypatch.setattr(health_routes, "check_migrations", bad)
    c = await make_client()
    err = assert_error(await c.get("/ready"), 503, "db_unavailable")
    checks = err["details"]["checks"]
    assert set(checks) == {"db", "migrations", "redis", "sim", "config"}
    assert checks["db"] == "error: ConnectionError" and checks["redis"] == "ok" and checks["sim"] == "ok"


async def test_ready_db_unavailable_real_connection_failure(make_client, redis_ok):
    c = await make_client()  # DATABASE_URL points at a closed port
    err = assert_error(await c.get("/ready"), 503, "db_unavailable")
    assert err["details"]["checks"]["db"].startswith("error")


async def test_ready_redis_unavailable(make_client, db_ok):
    c = await make_client()  # Redis stub is down
    err = assert_error(await c.get("/ready"), 503, "redis_unavailable")
    assert err["details"]["checks"]["redis"].startswith("error")
    assert err["details"]["checks"]["db"] == "ok"


@pytest.mark.parametrize("sim", [
    sim_with(exc=httpx.ConnectError("refused")),
    sim_with(exc=httpx.ReadTimeout("slow")),
    sim_with(status=500),
    sim_with(status=200, body={"status": "starting"}),
])
async def test_ready_sim_unavailable(make_client, db_ok, redis_ok, sim):
    c = await make_client(sim=sim)
    err = assert_error(await c.get("/ready"), 503, "sim_unavailable")
    assert err["details"]["checks"]["sim"].startswith("error")
    assert "AppError" not in err["details"]["checks"]["sim"]
    assert err["details"]["checks"]["db"] == "ok" == err["details"]["checks"]["redis"]


async def test_ready_config_error_prod_without_bot_token(make_client, db_ok, redis_ok):
    c = await make_client(app_env="prod", bot_token="", dev_auth=False)
    err = assert_error(await c.get("/ready"), 503, "config_error")
    assert err["details"]["checks"]["config"] == "error: missing BOT_TOKEN"


async def test_ready_config_error_when_urls_empty(make_client):
    c = await make_client(database_url="", redis_url="")
    err = assert_error(await c.get("/ready"), 503, "config_error")
    checks = err["details"]["checks"]
    assert "DATABASE_URL" in checks["config"] and "REDIS_URL" in checks["config"]
    assert checks["db"].startswith("skipped") and checks["redis"].startswith("skipped")


async def test_ready_prod_requires_explicit_env(make_client, db_ok, redis_ok, monkeypatch):
    from app.settings import Settings

    monkeypatch.delenv("SIM_URL", raising=False)
    s = Settings(app_env="prod", bot_token="x", database_url="postgresql://a/b", redis_url="redis://r/0")
    assert s.missing_required() == ["SIM_URL", "INTERNAL_TOKEN"]  # a dev default must not satisfy prod
    assert build_settings(app_env="prod", dev_auth=False).missing_required() == []


async def test_ready_error_priority(make_client, monkeypatch):
    """db > redis > sim > config; every failing check is listed regardless."""
    monkeypatch.setattr(health_routes, "check_db", bad)
    monkeypatch.setattr(health_routes, "check_migrations", ok)
    bad_sim = sim_with(status=500)

    c = await make_client(sim=bad_sim, app_env="prod", bot_token="", dev_auth=False)  # db+redis+sim+config fail
    err = assert_error(await c.get("/ready"), 503, "db_unavailable")
    assert [v.startswith("error") for v in err["details"]["checks"].values()].count(True) == 4

    monkeypatch.setattr(health_routes, "check_db", ok)
    err = assert_error(await c.get("/ready"), 503, "redis_unavailable")  # redis + sim + config
    assert err["details"]["checks"]["sim"].startswith("error")

    monkeypatch.setattr(health_routes, "check_redis", ok)
    assert_error(await c.get("/ready"), 503, "sim_unavailable")  # sim + config

    monkeypatch.setattr(health_routes, "check_sim", ok)
    assert_error(await c.get("/ready"), 503, "config_error")


async def test_ready_all_ok(make_client, clean_db, clean_redis):
    c = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL)
    resp = await c.get("/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ready", "checks": {
        "db": "ok", "migrations": "ok", "redis": "ok", "sim": "ok", "config": "ok"}}


async def test_ready_migrations_not_at_head(make_client, clean_db, clean_redis):
    c = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL)
    await clean_db.execute("UPDATE alembic_version SET version_num = 'stale'")
    try:
        err = assert_error(await c.get("/ready"), 503, "db_unavailable")
        assert err["details"]["checks"]["db"] == "ok"
        assert "not at head" in err["details"]["checks"]["migrations"]
    finally:
        await clean_db.execute(f"UPDATE alembic_version SET version_num = '{health_routes.alembic_head()}'")


async def test_ready_without_alembic_version_table(make_client, clean_db, clean_redis):
    c = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL)
    await clean_db.execute("ALTER TABLE alembic_version RENAME TO alembic_version_x")
    try:
        err = assert_error(await c.get("/ready"), 503, "db_unavailable")
        assert err["details"]["checks"]["migrations"].startswith("error")
    finally:
        await clean_db.execute("ALTER TABLE alembic_version_x RENAME TO alembic_version")
