"""FastAPI application factory. Run: `uvicorn app.main:create_app --factory`."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.gzip import GZipMiddleware

from .db import make_engine, make_redis, make_sessionmaker
from .errors import install_error_handlers
from .middleware import RequestContextMiddleware, setup_logging
from .routes import board, game, health, me
from .settings import Settings, get_settings
from .sim_client import SimClient

log = logging.getLogger("mosprom.app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await app.state.sim.aclose()
    if app.state.redis is not None:
        await app.state.redis.aclose()
    if app.state.engine is not None:
        await app.state.engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level)
    if settings.dev_auth and settings.is_prod:
        log.warning("DEV_AUTH is ignored because APP_ENV=prod")

    app = FastAPI(
        title="mosprom api", version="0.1.0", lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs", redoc_url=None,
        openapi_url=None if settings.is_prod else "/openapi.json",
    )
    # Clients are created eagerly but connect lazily, so the app also works without lifespan (ASGITransport in tests).
    app.state.settings = settings
    app.state.engine = make_engine(settings) if settings.database_url.strip() else None
    app.state.sessionmaker = make_sessionmaker(app.state.engine) if app.state.engine is not None else None
    app.state.redis = make_redis(settings) if settings.redis_url.strip() else None
    app.state.sim = SimClient(settings.sim_url, settings.sim_timeout_s)

    install_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)
    app.add_middleware(RequestContextMiddleware)  # outermost: request id, access log, last-resort 500

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(game.router)
    app.include_router(board.router)  # after game: /api/factory/me before /api/factory/{player_id}
    return app
