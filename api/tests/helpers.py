"""Constants and builders shared by tests (importable: `from tests.helpers import dev, tma`)."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

from app.errors import AppError
from app.ratelimit import rate_limit
from app.settings import Settings
from app.sim_client import SimClient
from app.testing import make_init_data

API_DIR = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://mosprom:mosprom@127.0.0.1:55433/mosprom_test")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56380/0")
DEAD_DB_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nodb"
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"
BOT_TOKEN = "123456:TEST-BOT-TOKEN"


class DeadRedis:
    """Stands in for an unreachable Redis: every operation raises immediately (a real dead port takes
    the connect timeout on Windows)."""

    def _down(self, *a, **k):
        raise RedisConnectionError("redis is down (test stub)")

    get = set = ping = incr = expire = ttl = execute = _down

    def pipeline(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        return None


def sim_ok_handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/health"
    return httpx.Response(200, json={"status": "ok", "service": "sim", "simCommit": "test"})


def mock_sim(handler=sim_ok_handler) -> SimClient:
    return SimClient("http://sim.test", 1.0, transport=httpx.MockTransport(handler))


class EchoIn(BaseModel):
    name: str
    count: int


def add_test_routes(app: FastAPI) -> None:
    """Routes that only exist in tests, to exercise generic error / limiter / gzip behaviour."""

    @app.get("/t/boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    @app.post("/t/echo")
    async def echo(body: EchoIn):
        return body

    @app.get("/t/limited", dependencies=[Depends(rate_limit("default"))])
    async def limited():
        return {"ok": True}

    @app.get("/t/apperror/{code}")
    async def apperror(code: str):
        raise AppError(code, "raised in test", details={"x": 1})

    @app.get("/t/big")
    async def big():
        return {"data": ["lorem ipsum dolor sit amet"] * 100}

    @app.get("/t/small")
    async def small():
        return {"ok": True}


def build_settings(**overrides) -> Settings:
    """Settings that ignore the developer's environment. Defaults: no reachable DB / Redis."""
    base = dict(app_env="dev", database_url=DEAD_DB_URL, redis_url=DEAD_REDIS_URL, sim_url="http://sim.test",
                sim_timeout_s=1.0, bot_token=BOT_TOKEN, dev_auth=True, init_data_max_age_s=86400,
                pgbouncer=False, log_level="INFO", rate_limit_overrides={}, rate_limit_enabled=True)
    return Settings(**{**base, **overrides})


def dev(tg_id: int = 1, name: str | None = None) -> dict[str, str]:
    return {"Authorization": f"dev {tg_id}" + (f":{name}" if name else "")}


def tma(user: dict | None = None, token: str = BOT_TOKEN, **kw) -> dict[str, str]:
    return {"Authorization": "tma " + make_init_data(token, user, **kw)}
