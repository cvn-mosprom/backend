"""Redis fixed-window rate limiter (INCR + EXPIRE), shared by all replicas.

Usage in a route:  `dependencies=[Depends(rate_limit("command"))]`  (declare it before the auth dependency
so unauthenticated floods are counted per IP). Fails open when Redis is unavailable.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Request

from .auth import get_identity
from .errors import AppError

log = logging.getLogger("mosprom.ratelimit")


@dataclass(frozen=True)
class Limit:
    limit: int
    window_s: int = 60
    burst: int = 0  # extra requests tolerated inside the window

    @property
    def max_requests(self) -> int:
        return self.limit + self.burst


# Spec "Блок 7" (per user per minute). `default` is for light routes without a dedicated limit.
LIMITS: dict[str, Limit] = {
    "bootstrap": Limit(10),
    "factory_me": Limit(60),
    "command": Limit(120, burst=20),
    "advance": Limit(10),
    "leaderboard": Limit(60),
    "events": Limit(10),
    "leads": Limit(5),
    "quiz": Limit(30),
    "default": Limit(60),
}


async def _subject(request: Request) -> str:
    """`u:<tg_id>` for an authenticated caller, else `ip:<addr>` (the route's own auth dependency
    still rejects the request; here we only need to count it)."""
    try:
        return f"u:{(await get_identity(request)).tg_id}"
    except AppError:
        return f"ip:{request.client.host if request.client else 'unknown'}"


async def enforce(request: Request, name: str) -> None:
    settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return
    limit = LIMITS[name]
    max_requests = settings.rate_limit_overrides.get(name, limit.max_requests)
    key = f"rl:{name}:{await _subject(request)}"
    try:
        async with request.app.state.redis.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, limit.window_s, nx=True)  # window starts at the first hit; never left without TTL
            pipe.ttl(key)
            count, _, ttl = await pipe.execute()
    except Exception as e:  # fail open: a Redis outage must not take the API down
        log.warning("rate limiter failing open (%s: %s)", type(e).__name__, name)
        return
    if count > max_requests:
        retry_after = ttl if isinstance(ttl, int) and ttl > 0 else limit.window_s
        raise AppError(
            "rate_limited", "Too many requests, slow down",
            details={"retry_after": retry_after, "limit": max_requests, "window_s": limit.window_s},
            headers={"Retry-After": str(retry_after)},
        )


def rate_limit(name: str) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency enforcing LIMITS[name]. Unknown names fail at import time."""
    if name not in LIMITS:
        raise KeyError(f"unknown rate limit {name!r}; known: {sorted(LIMITS)}")

    async def dependency(request: Request) -> None:
        await enforce(request, name)

    dependency.__name__ = f"rate_limit_{name}"
    return dependency
