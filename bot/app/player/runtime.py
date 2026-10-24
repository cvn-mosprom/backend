"""Process-wide handles of the player jobs. The Bot, the Settings and the DB sessionmaker are the HR runtime's (``hr.runtime.setup`` is
called once by ``main.amain``), so there is a single engine; this module only adds the Redis client of the 5xx alert.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..hr import runtime as hr_runtime

log = logging.getLogger(__name__)

_redis: Any = None
_redis_key: str | None = None


def current() -> hr_runtime.Runtime | None:
    """The HR runtime (settings, bot, sessionmaker); None before ``main`` set it up."""
    return hr_runtime.get()


def get_bot() -> Any:
    rt = hr_runtime.get()
    return rt.bot if rt is not None else None


def get_sessionmaker() -> async_sessionmaker[AsyncSession] | None:
    """None when DATABASE_URL is empty or the runtime is not set up (the features then idle)."""
    rt = hr_runtime.get()
    return rt.sm if rt is not None else None


def set_redis(redis: Any, key: str | None = "injected") -> None:
    global _redis, _redis_key
    _redis, _redis_key = redis, key


def get_redis(settings: Settings) -> Any:
    """Async Redis client (short timeouts: a stuck Redis must not stall the scheduler), None when REDIS_URL is empty."""
    global _redis, _redis_key
    if _redis_key != settings.redis_url:
        if settings.redis_url:
            from ..redis_client import create_redis

            _redis = create_redis(settings.redis_url)
        else:
            _redis = None
        _redis_key = settings.redis_url
    return _redis


async def close() -> None:
    global _redis, _redis_key
    redis, _redis, _redis_key = _redis, None, None
    closer = getattr(redis, "aclose", None)
    if closer is not None:
        try:
            await closer()
        except Exception as e:  # shutting down anyway
            log.warning("closing redis failed: %s: %s", type(e).__name__, e)
