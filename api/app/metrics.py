"""Per-minute request counters in Redis for the bot's 5xx alert (spec, Наблюдаемость: "счётчики в Redis").

`metrics:req:{YYYYmmddHHMM}` counts requests, `metrics:5xx:{YYYYmmddHHMM}` counts the ones answered with 5xx; the minute is UTC,
both keys expire after an hour. The bot's job (bot/app/alerts.py) sums the last 10 minutes. Probes (/health, /ready) and the bot's own
/internal/* calls are not counted: they would dilute (or fake) the error share. Fails open: Redis trouble never touches a request.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("mosprom.metrics")

REQ_PREFIX = "metrics:req:"
ERR_PREFIX = "metrics:5xx:"
TTL_S = 3600
REDIS_TIMEOUT_S = 0.25  # the counters must never slow a request down noticeably
UNCOUNTED_PATHS = ("/health", "/ready")
UNCOUNTED_PREFIXES = ("/internal/",)


def minute_stamp(at: datetime | None = None) -> str:
    return (at or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def counted(path: str) -> bool:
    return path not in UNCOUNTED_PATHS and not path.startswith(UNCOUNTED_PREFIXES)


async def record_request(redis: Any, path: str, status: int | None, at: datetime | None = None) -> bool:
    """INCR the minute counters of one finished request; True when written. Never raises (except cancellation)."""
    if redis is None or status is None or not counted(path):
        return False
    stamp = minute_stamp(at)
    try:
        async with asyncio.timeout(REDIS_TIMEOUT_S):
            async with redis.pipeline(transaction=False) as pipe:
                pipe.incr(REQ_PREFIX + stamp)
                pipe.expire(REQ_PREFIX + stamp, TTL_S)
                if status >= 500:
                    pipe.incr(ERR_PREFIX + stamp)
                    pipe.expire(ERR_PREFIX + stamp, TTL_S)
                await pipe.execute()
        return True
    except Exception as e:  # Redis down / slow: the alert simply sees fewer requests
        log.debug("request counter skipped: %s", type(e).__name__)
        return False
