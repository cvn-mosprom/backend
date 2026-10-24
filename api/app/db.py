"""Async engine / session factory / Redis client."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4
import ssl

import orjson
import redis.asyncio as aioredis
from redis.asyncio.sentinel import Sentinel
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .errors import AppError
from .settings import Settings


def connect_args(settings: Settings, timeouts: bool = True) -> dict[str, Any]:
    """asyncpg connect args. `timeouts=False` (migrations, one-off scripts) leaves statements unbounded.

    Timeouts (S7): direct Postgres gets `statement_timeout` / `lock_timeout` as session defaults at connect time
    (startup parameters). PgBouncer in transaction mode does not accept those startup parameters, so there the same
    bound is enforced client-side with asyncpg's `command_timeout` (cancels the running query on the server) and,
    in the pgbouncer container, by its own `QUERY_TIMEOUT` (see docker-compose.yml)."""
    args: dict[str, Any] = {"timeout": settings.db_connect_timeout_s}
    if settings.db_tls:
        args["ssl"] = ssl.create_default_context(cafile=settings.db_ca_file)
    if timeouts and settings.db_statement_timeout_ms > 0:
        if settings.pgbouncer:
            args["command_timeout"] = settings.db_statement_timeout_ms / 1000
        else:
            server_settings = {"statement_timeout": str(settings.db_statement_timeout_ms)}
            if settings.db_lock_timeout_ms > 0:
                server_settings["lock_timeout"] = str(settings.db_lock_timeout_ms)
            args["server_settings"] = server_settings
    if settings.pgbouncer:
        # transaction pooling: no server-side prepared statements may outlive a transaction
        args.update(
            statement_cache_size=0,  # asyncpg
            prepared_statement_cache_size=0,  # SQLAlchemy asyncpg dialect
            prepared_statement_name_func=lambda: f"__asyncpg_{uuid4()}__",
        )
    return args


def make_engine(settings: Settings, url: str | None = None, timeouts: bool = True, **kw: Any) -> AsyncEngine:
    if "poolclass" not in kw:  # default QueuePool; callers may pass e.g. NullPool (migrations)
        kw.setdefault("pool_size", settings.db_pool_size)
        kw.setdefault("max_overflow", settings.db_max_overflow)
        kw.setdefault("pool_timeout", 5)
    return create_async_engine(
        url or settings.database_url,
        pool_pre_ping=True,
        hide_parameters=True,  # personal data (leads) must not reach the logs inside DBAPI error messages
        connect_args=connect_args(settings, timeouts),
        json_serializer=lambda o: orjson.dumps(o).decode(),
        json_deserializer=orjson.loads,
        **kw,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def make_redis(settings: Settings) -> aioredis.Redis:
    # short timeouts: Redis is an optimisation layer (limiter / auth cache) and must fail fast
    if settings.redis_sentinel_nodes:
        nodes = [(host, int(port)) for host, port in
                 (node.strip().rsplit(":", 1) for node in settings.redis_sentinel_nodes.split(","))]
        security = {"ssl": True, "ssl_ca_certs": settings.redis_ca_file} if settings.redis_tls else {}
        sentinel = Sentinel(nodes, socket_connect_timeout=0.5, socket_timeout=1.0,
                            decode_responses=True, sentinel_kwargs={**security, "password": settings.redis_sentinel_password or None,
                                                                  "socket_connect_timeout": 0.5, "socket_timeout": 1.0})
        return sentinel.master_for(settings.redis_master_name, password=settings.redis_password or None, **security)
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
