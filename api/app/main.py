"""FastAPI application factory. Run: `uvicorn app.main:create_app --factory`."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from .db import make_engine, make_redis, make_sessionmaker
from .errors import install_error_handlers
from .middleware import RequestContextMiddleware, setup_logging
from .routes import board, events, game, health, internal, leads, me, quiz
from .settings import Settings, get_settings
from .sim_client import SimClient

log = logging.getLogger("mosprom.app")

# http-contract.md "Общее": the web client sends X-Telegram-Init-Data / X-Dev-User / Content-Type and reads Retry-After
CORS_METHODS = ["GET", "POST", "DELETE"]
CORS_HEADERS = ["X-Telegram-Init-Data", "X-Dev-User", "Content-Type", "Authorization"]
CORS_EXPOSE = ["Retry-After", "X-Request-ID"]
CORS_MAX_AGE = 600


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: Settings = app.state.settings
    if cfg.is_prod and not cfg.internal_token_ok:  # S7: the bot's /internal/* calls must never run on a guessable token
        log.critical("refusing to start: INTERNAL_TOKEN is missing, too short (< 16) or a placeholder (APP_ENV=prod)")
        raise RuntimeError("INTERNAL_TOKEN is required when APP_ENV=prod")
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
    app.state.sim = SimClient(settings.sim_url, settings.sim_timeout_s, breaker_threshold=settings.sim_breaker_threshold,
                              breaker_open_s=settings.sim_breaker_open_s)

    install_error_handlers(app)
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=5)
    # outermost: request id, access log, 5 s request cap (not /internal/*), slow-request log, last-resort 500
    app.add_middleware(RequestContextMiddleware, request_timeout_s=settings.request_timeout_s,
                       slow_ms=settings.slow_request_ms)
    # S8: added LAST = outermost, so 4xx / 429 / 5xx / timeout answers built by the layers below still carry CORS headers
    # (else the browser reports them as a bare "network error"), and a preflight OPTIONS is answered here, before
    # auth, the rate limiter and the request timeout ever run.
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_methods=CORS_METHODS,
                       allow_headers=CORS_HEADERS, expose_headers=CORS_EXPOSE, max_age=CORS_MAX_AGE)

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(game.router)
    app.include_router(board.router)  # after game: /api/factory/me before /api/factory/{player_id}
    app.include_router(leads.router)  # S5: independent of the sim (leads, quiz, events)
    app.include_router(quiz.router)
    app.include_router(events.router)
    app.include_router(internal.router)  # /internal/* for the bot's jobs: token-protected, not proxied by caddy
    return app
