"""Async engine for the HR features (lazy: nothing connects until the first query)."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import Settings

log = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 10_000  # analytics may scan events; still bounded
LOCK_TIMEOUT_MS = 5_000


def normalize_url(url: str) -> str:
    """The bot talks asyncpg only: accept the plain postgresql:// / postgres:// forms too."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def connect_args(pgbouncer: bool) -> dict[str, Any]:
    """Same semantics as api/app/db.py: transaction pooling (PGBOUNCER=1) -> no prepared statement caches and a
    client-side command_timeout instead of startup parameters (pgbouncer rejects those)."""
    args: dict[str, Any] = {"timeout": 5}
    if pgbouncer:
        args.update(
            command_timeout=STATEMENT_TIMEOUT_MS / 1000,
            statement_cache_size=0,  # asyncpg
            prepared_statement_cache_size=0,  # SQLAlchemy asyncpg dialect
            prepared_statement_name_func=lambda: f"__asyncpg_{uuid4()}__",
        )
    else:
        args["server_settings"] = {
            "statement_timeout": str(STATEMENT_TIMEOUT_MS), "lock_timeout": str(LOCK_TIMEOUT_MS),
            "application_name": "mosprom-bot",
        }
    return args


def make_engine(url: str, pgbouncer: bool = False) -> AsyncEngine:
    return create_async_engine(
        normalize_url(url), pool_size=3, max_overflow=2, pool_timeout=5, pool_pre_ping=True,
        hide_parameters=True,  # leads are personal data: no SQL parameters in error messages / logs
        connect_args=connect_args(pgbouncer),
    )


def make_sessionmaker(settings: Settings) -> async_sessionmaker[AsyncSession] | None:
    if not settings.database_url:
        return None
    return async_sessionmaker(make_engine(settings.database_url, settings.pgbouncer), expire_on_commit=False)
