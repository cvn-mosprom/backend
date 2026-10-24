"""Achievement notifications (C2): the api inserts an ``achievement_unlocked`` outbox row in the transaction that grants the
achievement; this job tells the player in the bot. Decisions (docs/hr-crm-plan.md "Решения" 2):

* only gold and seasonal achievements are announced in the bot (the game itself shows a toast for every one);
* only to players who started the bot (``users.bot_started_at``) and kept the notifications on (``users.notify_opt_in``);
* never for demo bots / test users (``is_bot`` / ``is_test``), nor for a user that no longer exists;
* no messages in the quiet hours 23:00-09:00 MSK (the rows simply wait), nothing older than ``STALE_H`` hours;
* every row ends up with ``sent_at`` set: delivered, or skipped with the reason in ``last_error`` (so the table does not grow a
  backlog of rows nobody will ever send). A failed send is retried with an in-memory backoff, at most ``MAX_ATTEMPTS`` times; a
  player who blocked the bot is opted out (like the order reminders).
Rows are claimed one at a time with ``FOR UPDATE SKIP LOCKED`` so two workers never send the same message.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import text

from . import runtime, store
from .keyboards import build_start_keyboard, is_webapp_url_ok
from .reminders import SEND_INTERVAL_S, is_quiet

log = logging.getLogger(__name__)

KIND = "achievement_unlocked"
NOTIFY_TIERS = frozenset({"gold"})
MAX_ATTEMPTS = 20
BATCH = 100  # rows per run
STALE_H = 48
BUDGET_S = 25.0
BACKOFF_BASE_S, BACKOFF_MAX_S = 10, 300
TIER_RU = {"gold": "золото", "silver": "серебро", "bronze": "бронза"}

_next_try: dict[int, float] = {}  # outbox id -> time.monotonic() before which it is not retried


def notifiable(payload: dict[str, Any]) -> bool:
    """Gold or seasonal (the player-facing rule); everything else is skipped without a message."""
    return payload.get("tier") in NOTIFY_TIERS or payload.get("seasonal") is True


def skip_reason(payload: Any, user: Any, created_at: datetime | None, now: datetime) -> str | None:
    """Why a row must not be sent (None = send it). `user` carries is_bot, is_test, notify_opt_in, bot_started_at, tg_id."""
    if not isinstance(payload, dict) or not payload.get("title"):
        return "malformed payload"
    if user is None or user.uid is None:
        return "no such user"
    if user.is_bot or user.is_test:
        return "bot or test user"
    if not notifiable(payload):
        return "not gold or seasonal"
    if not user.notify_opt_in or user.bot_started_at is None:
        return "not opted in"
    if not isinstance(user.tg_id, int) or user.tg_id <= 0:
        return "no chat"
    if created_at is not None and now - created_at > timedelta(hours=STALE_H):
        return "stale"
    return None


def message_text(payload: dict[str, Any]) -> str:
    tier = TIER_RU.get(str(payload.get("tier")), "")
    points = payload.get("points")
    meta = " · ".join(p for p in (("сезонное" if payload.get("seasonal") else ""), tier,
                                   f"+{points} ОД" if isinstance(points, int) and not isinstance(points, bool) else "") if p)
    return (f"🏆 <b>Новое достижение!</b>\n«{html.escape(str(payload.get('title')))}»" + (f"\n{meta}" if meta else "")
            + "\n\nОтключить уведомления: /stop")


def backoff_s(attempts: int) -> float:
    return min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** max(attempts - 1, 0))


async def _send(bot: Any, chat_id: int, body: str, markup: Any, sleep: Callable[[float], Awaitable[Any]]) -> None:
    try:
        await bot.send_message(chat_id, body, reply_markup=markup)
    except TelegramRetryAfter as e:  # flood wait: once, then whatever the second attempt raises
        await sleep(min(float(e.retry_after), 30.0))
        await bot.send_message(chat_id, body, reply_markup=markup)


_CLAIM = text(
    "SELECT o.id, o.payload, o.attempts, o.created_at, u.id AS uid, u.tg_id, u.is_bot, u.is_test, u.notify_opt_in, "
    "u.bot_started_at FROM outbox o LEFT JOIN users u ON u.id = "
    "CASE WHEN jsonb_typeof(o.payload -> 'user_id') = 'number' THEN (o.payload ->> 'user_id')::bigint END "
    "WHERE o.kind = :kind AND o.sent_at IS NULL AND o.attempts < :max AND NOT (o.id = ANY(CAST(:skip AS bigint[]))) "
    "ORDER BY o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED")


async def run_notifications(bot: Any, sm: store.SessionMaker, settings: Any, *, now: datetime | None = None,
                            sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep, limit: int = BATCH) -> dict[str, int]:
    """One pass over the pending rows; returns counters (also for tests). Never raises for one row's trouble."""
    now = now or datetime.now(timezone.utc)
    stats = {"sent": 0, "skipped": 0, "failed": 0, "blocked": 0, "quiet": 0}
    if is_quiet(now):
        stats["quiet"] = 1
        return stats
    play_url = settings.webapp_url if is_webapp_url_ok(settings.webapp_url) else None
    started = time.monotonic()
    for _ in range(limit):
        if time.monotonic() - started > BUDGET_S:
            break
        wait = [i for i, t in _next_try.items() if t > time.monotonic()]
        async with sm() as session, session.begin():
            row = (await session.execute(_CLAIM, {"kind": KIND, "max": MAX_ATTEMPTS, "skip": wait})).first()
            if row is None:
                break
            reason = skip_reason(row.payload, row if row.uid is not None else None, row.created_at, now)
            if reason is not None:
                await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = :err WHERE id = :id"),
                                      {"err": f"not delivered: {reason}", "id": row.id})
                stats["skipped"] += 1
                continue
            try:
                await _send(bot, row.tg_id, message_text(row.payload), build_start_keyboard(play_url, True), sleep)
            except TelegramForbiddenError:  # blocked the bot: stop asking
                await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = 'not delivered: blocked the bot' "
                                           "WHERE id = :id"), {"id": row.id})
                await session.execute(text("UPDATE users SET notify_opt_in = false, notify_opt_out_at = now() "
                                           "WHERE id = :uid AND notify_opt_in"), {"uid": row.uid})
                stats["blocked"] += 1
                continue
            except (TelegramAPIError, OSError, asyncio.TimeoutError) as e:
                err = f"{type(e).__name__}: {str(e)[:150]}"
                await session.execute(text("UPDATE outbox SET attempts = attempts + 1, last_error = :err WHERE id = :id"),
                                      {"err": err, "id": row.id})
                _next_try[row.id] = time.monotonic() + backoff_s(row.attempts + 1)
                stats["failed"] += 1
                log.warning("achievement notification %s to user %s failed: %s", row.id, row.uid, err)
                break  # Telegram is probably unreachable: do not burn the attempts of the other rows
            await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = NULL WHERE id = :id"), {"id": row.id})
            _next_try.pop(row.id, None)
            stats["sent"] += 1
        await sleep(SEND_INTERVAL_S)
    return stats


async def achievement_notify() -> dict[str, int] | None:
    """Scheduled job (every 20 s). Idle without BOT_TOKEN or DATABASE_URL; never raises."""
    rt = runtime.current()
    if rt is None or rt.bot is None:
        return None
    if rt.sm is None:
        log.warning("achievement_notify skipped: DATABASE_URL is not set")
        return None
    try:
        stats = await run_notifications(rt.bot, rt.sm, rt.settings)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # DB restarting, ...: the next tick retries
        log.warning("achievement_notify failed: %s: %s", type(e).__name__, e)
        return None
    if stats["sent"] or stats["failed"] or stats["blocked"]:
        log.info("achievement_notify: %s", stats)
    return stats
