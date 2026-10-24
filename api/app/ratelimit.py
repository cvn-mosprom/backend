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
    "routing_report": Limit(10),
    "bootstrap": Limit(10),
    "factory_me": Limit(60),
    "command": Limit(120, burst=20),
    "advance": Limit(10, burst=2),  # the web client keeps <= 8/min (heartbeat 30 s + order_due); never below 10/min
    "leaderboard": Limit(60),
    "events": Limit(10, burst=6),  # the web client flushes every 5 s = 12/min (+ a flush on close): 16 fit
    "leads": Limit(5),
    "quiz": Limit(30),
    "auth": Limit(5),  # S14 web login (POST /api/auth/web/code, /start): per IP, whatever Authorization header is sent
    "auth_poll": Limit(60, burst=20),  # S14 POST /api/auth/web/poll: the page polls every ~2 s (30/min); per IP
    "hr": Limit(120),  # C3: every /api/hr/* route (the CRM web client), per user
    "default": Limit(60),
}
IP_LIMITS = frozenset({"auth", "auth_poll"})  # unauthenticated endpoints: always counted per IP, never per user


async def _subject(request: Request, name: str = "") -> str:
    """`u:<tg_id>` for an authenticated caller, else `ip:<addr>` (the route's own auth dependency
    still rejects the request; here we only need to count it). `IP_LIMITS` names always use the IP."""
    if name in IP_LIMITS:
        return f"ip:{request.client.host if request.client else 'unknown'}"
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
    key = f"rl:{name}:{await _subject(request, name)}"
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
