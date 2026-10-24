"""Shared fixtures.

Postgres / Redis tests use TEST_DATABASE_URL / TEST_REDIS_URL (defaults from docs/ARCHITECTURE.md) and are
SKIPPED with a reason when the service is unreachable. Everything else runs hermetically.

Throwaway containers (the defaults above point at these):
    docker run -d --name mosprom-test-pg -e POSTGRES_USER=mosprom -e POSTGRES_PASSWORD=mosprom
        -e POSTGRES_DB=mosprom_test -p 127.0.0.1:55433:5432 postgres:16-alpine
    docker run -d --name mosprom-test-redis -p 127.0.0.1:56380:6379 redis:7-alpine
Run: `py -3.12 -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt && .venv/Scripts/python -m pytest`
(from api/). The schema is reset and migrated with `alembic upgrade head` once per session.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import AsyncExitStack

import asyncpg
import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport

from app.main import create_app
from app.sim_client import SimClient
from tests.helpers import (API_DIR, DEAD_REDIS_URL, TEST_DB_URL, TEST_REDIS_URL, DeadRedis, add_test_routes,
                           build_settings, mock_sim)

DOCKER_HINT = "start the containers listed in the tests/conftest.py docstring"
ENV_VARS = ["APP_ENV", "DATABASE_URL", "REDIS_URL", "SIM_URL", "SIM_TIMEOUT_S", "BOT_TOKEN", "DEV_AUTH",
            "INIT_DATA_MAX_AGE_S", "PGBOUNCER", "DOMAIN", "LOG_LEVEL", "RATE_LIMIT_ENABLED",
            "RATE_LIMIT_OVERRIDES", "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "INTERNAL_TOKEN", "REQUEST_TIMEOUT_S",
            "SLOW_REQUEST_MS", "DB_STATEMENT_TIMEOUT_MS", "DB_LOCK_TIMEOUT_MS", "SIM_BREAKER_THRESHOLD",
            "SIM_BREAKER_OPEN_S", "CORS_ORIGINS", "WEBAPP_URL"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def pg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


# ---------------------------------------------------------------- real services (skip when absent)

@pytest.fixture(scope="session")
def pg_url() -> str:
    """Test database, reset and migrated with the real CLI (`alembic upgrade head` from api/)."""
    async def reset() -> None:
        conn = await asyncpg.connect(pg_dsn(TEST_DB_URL), timeout=3)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    try:
        asyncio.run(reset())
    except Exception as e:
        pytest.skip(f"Postgres not reachable at TEST_DATABASE_URL ({type(e).__name__}); {DOCKER_HINT}")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_DIR, check=True,
                   env={**os.environ, "DATABASE_URL": TEST_DB_URL}, capture_output=True)
    return TEST_DB_URL


@pytest.fixture(scope="session")
def redis_url() -> str:
    async def ping() -> None:
        r = aioredis.from_url(TEST_REDIS_URL, socket_connect_timeout=2)
        try:
            await r.ping()
        finally:
            await r.aclose()

    try:
        asyncio.run(ping())
    except Exception as e:
        pytest.skip(f"Redis not reachable at TEST_REDIS_URL ({type(e).__name__}); {DOCKER_HINT}")
    return TEST_REDIS_URL


@pytest_asyncio.fixture
async def clean_db(pg_url):
    """Empty tables (schema stays migrated); yields a raw asyncpg connection for assertions."""
    conn = await asyncpg.connect(pg_dsn(pg_url))
    await conn.execute("TRUNCATE factories, seasons, users, outbox RESTART IDENTITY CASCADE")
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def clean_redis(redis_url):
    r = aioredis.from_url(redis_url, decode_responses=True)
    await r.flushdb()
    yield r
    await r.aclose()


# ---------------------------------------------------------------- app / client factories

@pytest_asyncio.fixture
async def make_client():
    """`await make_client(sim=SimClient, **settings_overrides)` -> httpx client with `.app`.
    Defaults: unreachable Postgres, Redis down (limiter / auth cache fail open), healthy mocked sim."""
    stack = AsyncExitStack()

    async def _make(sim: SimClient | None = None, **overrides) -> httpx.AsyncClient:
        app = create_app(build_settings(**overrides))
        add_test_routes(app)
        if app.state.settings.redis_url == DEAD_REDIS_URL:
            app.state.redis = DeadRedis()
        app.state.sim = sim or mock_sim()
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"))
        client.app = app  # type: ignore[attr-defined]
        return client

    yield _make
    await stack.aclose()


@pytest_asyncio.fixture
async def live(make_client, clean_db, clean_redis):
    """Client wired to the throwaway Postgres + Redis (both emptied). `live.db` = asyncpg connection."""
    client = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL)
    client.db = clean_db  # type: ignore[attr-defined]
    client.redis = clean_redis  # type: ignore[attr-defined]
    return client


# ---------------------------------------------------------------- game endpoints (fake sim)

@pytest.fixture
def fake_sim():
    from tests.fake_sim import FakeSim
    return FakeSim()


@pytest_asyncio.fixture
async def game(make_client, clean_db, clean_redis, fake_sim):
    """Client on the throwaway Postgres + Redis with the in-process fake sim and the rate limiter OFF.
    `.db` asyncpg connection, `.redis`, `.sim` the FakeSim."""
    client = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                               rate_limit_enabled=False)
    client.db = clean_db  # type: ignore[attr-defined]
    client.redis = clean_redis  # type: ignore[attr-defined]
    client.sim = fake_sim  # type: ignore[attr-defined]
    return client
