"""GET /health (liveness) and GET /ready (dependency checks for orchestration)."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from functools import lru_cache
from pathlib import Path

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..errors import AppError

router = APIRouter()

ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "alembic"
CHECK_TIMEOUT_S = 2.0
# check name -> error code, in the priority order used to pick the response code
CODE_ORDER = [
    ("db", "db_unavailable"), ("migrations", "db_unavailable"), ("redis", "redis_unavailable"),
    ("sim", "sim_unavailable"), ("config", "config_error"),
]


@lru_cache
def alembic_head() -> str | None:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return ScriptDirectory.from_config(cfg).get_current_head()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "api", "serverTime": int(time.time() * 1000)}


async def _guard(coro: Awaitable[str]) -> str:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            return await coro
    except TimeoutError:
        return "error: timeout"
    except AppError as e:  # e.g. sim_unavailable carries a short machine-readable reason
        reason = e.details.get("reason") or (f"upstream status {e.details['upstream_status']}"
                                             if "upstream_status" in e.details else e.code)
        return f"error: {reason}"
    except Exception as e:  # class name only: messages may carry hostnames / DSNs
        return f"error: {type(e).__name__}"


async def check_db(request: Request) -> str:
    sm = request.app.state.sessionmaker
    if sm is None:
        return "skipped: DATABASE_URL missing"
    async with sm() as session:
        await session.execute(text("SELECT 1"))
    return "ok"


async def check_migrations(request: Request) -> str:
    sm = request.app.state.sessionmaker
    if sm is None:
        return "skipped: DATABASE_URL missing"
    head = alembic_head()
    async with sm() as session:
        rows = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    if rows != [head]:
        return f"error: not at head (current={rows[0] if rows else None}, head={head})"
    return "ok"


async def check_redis(request: Request) -> str:
    if request.app.state.redis is None:
        return "skipped: REDIS_URL missing"
    await request.app.state.redis.ping()
    return "ok"


async def check_sim(request: Request) -> str:
    data = await request.app.state.sim.health(timeout=CHECK_TIMEOUT_S)
    return "ok" if data.get("status") == "ok" else f"error: status={data.get('status')!r}"


MEDIA_PROBE_PATH = "/audio/samples.json"  # a small object that must exist once media-sync has run


async def check_media(request: Request) -> str:
    """S15: HEAD of one small public media object in SeaweedFS. INFORMATIONAL: never part of CODE_ORDER, so a media outage
    shows up under `checks.media` but does not make /ready fail (the game itself does not depend on the media server)."""
    base = request.app.state.settings.media_url.rstrip("/")
    if not base:
        return "skipped: MEDIA_URL missing"
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S) as c:
        r = await c.head(base + MEDIA_PROBE_PATH)
    return "ok" if r.status_code == 200 else f"error: upstream status {r.status_code}"


def check_config(request: Request) -> str:
    missing = request.app.state.settings.missing_required()
    return f"error: missing {', '.join(missing)}" if missing else "ok"


@router.get("/ready")
async def ready(request: Request):
    names = ["db", "migrations", "redis", "sim", "media"]
    results = await asyncio.gather(*(_guard(c(request)) for c in (check_db, check_migrations, check_redis, check_sim, check_media)))
    checks = dict(zip(names, results))
    checks["config"] = check_config(request)

    failed = [(n, code) for n, code in CODE_ORDER if checks[n].startswith("error")]
    if failed:
        raise AppError(failed[0][1], f"Service is not ready: {', '.join(n for n, _ in failed)}",
                       details={"checks": checks})
    return JSONResponse({"status": "ready", "checks": checks})
