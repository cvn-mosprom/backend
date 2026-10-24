"""5xx alert (spec, Наблюдаемость + S6): every minute the bot sums the api's per-minute request counters of the last 10 minutes
(Redis ``metrics:req:{YYYYmmddHHMM}`` and ``metrics:5xx:{...}``, UTC minutes, written by api/app/metrics.py); when there were at least
``MIN_REQUESTS`` requests and more than ``ERROR_SHARE`` of them ended with 5xx, the HR admins (HR_ADMIN_TG_IDS) get one message with
the numbers. After a delivered alert the next one waits ``COOLDOWN_S`` (kept in memory: the bot is the only owner of the scheduler,
and a restart merely allows one extra alert; Redis roles stay as the spec lists them). Without BOT_TOKEN / REDIS_URL the job does nothing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram.exceptions import TelegramAPIError

from . import runtime

log = logging.getLogger(__name__)

REQ_PREFIX = "metrics:req:"
ERR_PREFIX = "metrics:5xx:"
WINDOW_MIN = 10
MIN_REQUESTS = 20
ERROR_SHARE = 0.20
COOLDOWN_S = 15 * 60


def window_stamps(now: datetime, minutes: int = WINDOW_MIN) -> list[str]:
    """UTC minute stamps of the current minute and the `minutes - 1` before it."""
    now = now.astimezone(timezone.utc)
    return [(now - timedelta(minutes=i)).strftime("%Y%m%d%H%M") for i in range(minutes)]


def is_storm(total: int, errors: int) -> bool:
    """>= MIN_REQUESTS requests and strictly more than 20 % of them 5xx (exactly 20 % is not an alert)."""
    return total >= MIN_REQUESTS and errors > ERROR_SHARE * total


async def read_window(redis: Any, now: datetime, minutes: int = WINDOW_MIN) -> tuple[int, int]:
    """(requests, 5xx) summed over the last `minutes` minutes."""
    stamps = window_stamps(now, minutes)
    values = await redis.mget([REQ_PREFIX + s for s in stamps] + [ERR_PREFIX + s for s in stamps])

    def total(part: list[Any]) -> int:
        return sum(int(v) for v in part if v is not None and str(v).lstrip("-").isdigit())

    return total(values[:minutes]), total(values[minutes:])


def alert_text(total: int, errors: int) -> str:
    share = 100 * errors / total
    return (f"🚨 <b>Всплеск ошибок 5xx</b>\nЗа последние {WINDOW_MIN} мин: {errors} из {total} запросов к api "
            f"({share:.0f}%) закончились ошибкой 5xx (порог {ERROR_SHARE:.0%}).\n"
            f"Проверь api и sim: <code>docker compose logs --tail 200 api sim</code>.\n"
            f"Следующее предупреждение не раньше чем через {COOLDOWN_S // 60} мин.")


class AlertState:
    """When the last alert was delivered (monotonic clock; `clock` is injectable for tests)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.last_sent: float | None = None

    def cooling_down(self) -> bool:
        return self.last_sent is not None and self.clock() - self.last_sent < COOLDOWN_S

    def mark_sent(self) -> None:
        self.last_sent = self.clock()


STATE = AlertState()


async def check_5xx(bot: Any, redis: Any, admin_ids: frozenset[int] | set[int], *, now: datetime | None = None,
                    state: AlertState | None = None) -> dict[str, Any]:
    """One check; returns what happened: {total, errors, storm, alerted, sent_to}."""
    state = state or STATE
    now = now or datetime.now(timezone.utc)
    total, errors = await read_window(redis, now)
    result: dict[str, Any] = {"total": total, "errors": errors, "storm": is_storm(total, errors), "alerted": False,
                              "sent_to": 0}
    if not result["storm"] or state.cooling_down():
        return result
    if not admin_ids:
        log.warning("5xx storm (%s of %s requests in %s min) but HR_ADMIN_TG_IDS is empty: nobody to alert", errors,
                    total, WINDOW_MIN)
        state.mark_sent()  # one warning per cooldown, not one per minute
        return result
    text = alert_text(total, errors)
    for chat_id in sorted(admin_ids):
        try:
            await bot.send_message(chat_id, text)
            result["sent_to"] += 1
        except TelegramAPIError as e:
            log.warning("5xx alert to %s failed: %s: %s", chat_id, type(e).__name__, e)
    if result["sent_to"]:
        state.mark_sent()
        result["alerted"] = True
        log.warning("5xx alert sent to %s admin(s): %s of %s requests", result["sent_to"], errors, total)
    return result


async def alert_5xx() -> dict[str, Any] | None:
    """Scheduled job (every minute). Idle without BOT_TOKEN or REDIS_URL; never raises."""
    rt = runtime.current()
    if rt is None or rt.bot is None:
        return None
    redis = runtime.get_redis(rt.settings)
    if redis is None:
        return None
    try:
        return await check_5xx(rt.bot, redis, await rt.admin_ids())
    except Exception as e:  # Redis restarting, Telegram down ...: the next minute retries
        log.warning("alert_5xx failed: %s: %s", type(e).__name__, e)
        return None
