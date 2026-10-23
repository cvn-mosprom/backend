"""Async engine / session factory / Redis client."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import orjson
import redis.asyncio as aioredis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .errors import AppError
from .settings import Settings


def connect_args(settings: Settings) -> dict[str, Any]:
    args: dict[str, Any] = {"timeout": settings.db_connect_timeout_s}
    if settings.pgbouncer:
        # transaction pooling: no server-side prepared statements may outlive a transaction
        args.update(
            statement_cache_size=0,  # asyncpg
            prepared_statement_cache_size=0,  # SQLAlchemy asyncpg dialect
            prepared_statement_name_func=lambda: f"__asyncpg_{uuid4()}__",
        )
    return args


def make_engine(settings: Settings, url: str | None = None, **kw: Any) -> AsyncEngine:
    if "poolclass" not in kw:  # default QueuePool; callers may pass e.g. NullPool (migrations)
        kw.setdefault("pool_size", settings.db_pool_size)
        kw.setdefault("max_overflow", settings.db_max_overflow)
        kw.setdefault("pool_timeout", 5)
    return create_async_engine(
        url or settings.database_url,
        pool_pre_ping=True,
        connect_args=connect_args(settings),
        json_serializer=lambda o: orjson.dumps(o).decode(),
        json_deserializer=orjson.loads,
        **kw,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def make_redis(settings: Settings) -> aioredis.Redis:
    # short timeouts: Redis is an optimisation layer (limiter / auth cache) and must fail fast
    return aioredis.from_url(
        settings.redis_url, decode_responses=True,
        socket_connect_timeout=0.5, socket_timeout=1.0, health_check_interval=30,
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Per-request session. Callers manage transactions: `async with session.begin(): ...`."""
    sm = request.app.state.sessionmaker
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    async with sm() as session:
        yield session
