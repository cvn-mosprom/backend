"""Order reminders (spec, Блок 5): consenting players get a message when an active order is about to finish ("eta") or its
deadline is burning ("deadline"). Nothing here depends on a tick: only moments stored in the last snapshot are used.

Source: ``factories.snapshot -> orders`` (read-only). Order fields used (vendor types.ts ``Order``): ``id``, ``client``, ``status``
(accepted | loading are active), ``tutorial`` (never reminded: it has no real deadline), ``loaded`` / ``qty``, ``deadline`` (ms epoch) and
the server completion moment. That moment is not in the vendored type yet (S1 introduces it as an optional field), so the first
numeric one of ``ETA_FIELDS`` is taken; without it only deadline reminders exist. Missing / malformed fields skip the order, never
the run.

* ``eta``      eta within ``ETA_WINDOW`` (15 min) and not later than the deadline (the order does finish in time).
* ``deadline`` deadline within ``DEADLINE_WINDOW`` (60 min) and the order is not going to finish before it (eta unknown or later).

Each (player, order, kind) is reminded at most once (table ``reminders_sent``, claimed BEFORE the send and released when the send
fails). No reminders in quiet hours 23:00-09:00 MSK (they are simply retried by the next run), at most ~25 sends per second.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter

from ..config import Settings
from .keyboards import build_start_keyboard, is_webapp_url_ok
from . import runtime, store

log = logging.getLogger(__name__)

ACTIVE_STATUSES = frozenset({"accepted", "loading"})
ETA_FIELDS = ("etaAt", "completesAt", "completeAt", "finishAt")
ETA_WINDOW_MS = 15 * 60_000
DEADLINE_WINDOW_MS = 60 * 60_000
MSK = timezone(timedelta(hours=3))
QUIET_FROM_H, QUIET_TO_H = 23, 9  # MSK, [23:00, 09:00)
SEND_INTERVAL_S = 0.04  # ~25 messages per second (Telegram allows ~30)
MAX_LINES = 5
PAGE = 200

KIND_ETA, KIND_DEADLINE = "eta", "deadline"


@dataclass(frozen=True)
class Due:
    order_id: str
    kind: str
    client: str
    minutes: int  # minutes until the eta / deadline (>= 1)
    loaded: int | None = None
    qty: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.order_id, self.kind)


def _ms(v: Any) -> float | None:
    """A plausible epoch-ms moment, else None (bool, strings, NaN, zero and far-future garbage are ignored)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) and 0 < v < 4e12 else None


def _count(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 else None


def order_eta(order: dict[str, Any]) -> float | None:
    for name in ETA_FIELDS:
        eta = _ms(order.get(name))
        if eta is not None:
            return eta
    return None


def due_reminders(orders: Any, now_ms: float) -> list[Due]:
    """Reminders that are due for one player's order list at `now_ms` (pure; tolerant to missing / odd fields)."""
    if not isinstance(orders, list):
        return []
    out: list[Due] = []
    for o in orders:
        if not isinstance(o, dict) or o.get("status") not in ACTIVE_STATUSES or o.get("tutorial") is True:
            continue
        oid = o.get("id")
        if not isinstance(oid, str) or not oid:
            continue
        eta, deadline = order_eta(o), _ms(o.get("deadline"))
        kind, moment = None, None
        if eta is not None and now_ms < eta <= now_ms + ETA_WINDOW_MS and (deadline is None or eta <= deadline):
            kind, moment = KIND_ETA, eta
        elif (deadline is not None and now_ms < deadline <= now_ms + DEADLINE_WINDOW_MS
              and (eta is None or eta > deadline)):
            kind, moment = KIND_DEADLINE, deadline
        if kind is None or moment is None:
            continue
        client = o.get("client")
        out.append(Due(oid, kind, client.strip() if isinstance(client, str) and client.strip() else "без названия",
                       max(1, math.ceil((moment - now_ms) / 60_000)), _count(o.get("loaded")), _count(o.get("qty"))))
    out.sort(key=lambda d: (d.minutes, d.order_id, d.kind))
    return out


def is_quiet(now: datetime) -> bool:
    """23:00-09:00 Moscow time (UTC+3, no DST)."""
    h = now.astimezone(MSK).hour
    return h >= QUIET_FROM_H or h < QUIET_TO_H


def reminder_text(due: list[Due]) -> str:
    lines = []
    for d in due[:MAX_LINES]:
        name = f"«{html.escape(d.client)}»"
        progress = f" ({d.loaded}/{d.qty})" if d.loaded is not None and d.qty else ""
        if d.kind == KIND_ETA:
            lines.append(f"📦 Заказ {name} будет готов через ~{d.minutes} мин.{progress}")
        else:
            lines.append(f"⏰ Горит срок заказа {name}: осталось ~{d.minutes} мин.{progress}")
    if len(due) > MAX_LINES:
        lines.append(f"…и ещё заказов: {len(due) - MAX_LINES}")
    return "\n".join(lines) + "\n\nОтключить напоминания: /stop"


async def _send(bot: Any, chat_id: int, text: str, markup: Any, sleep: Callable[[float], Awaitable[Any]]) -> None:
    """One message; on a flood-wait waits once and retries. Raises what the second attempt raises."""
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
    except TelegramRetryAfter as e:
        await sleep(min(float(e.retry_after), 30.0))
        await bot.send_message(chat_id, text, reply_markup=markup)


async def run_reminders(bot: Any, sm: store.SessionMaker, settings: Settings, *, now: datetime | None = None,
                        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> dict[str, int]:
    """One pass over the opted-in players; returns counters (also for tests). Never raises for one player's trouble."""
    now = now or datetime.now(timezone.utc)
    stats = {"users": 0, "sent": 0, "skipped_dup": 0, "failed": 0, "blocked": 0, "quiet": 0}
    if is_quiet(now):
        stats["quiet"] = 1
        return stats
    now_ms = now.timestamp() * 1000
    play_url = settings.webapp_url if is_webapp_url_ok(settings.webapp_url) else None
    after = 0
    while True:
        page = await store.opted_in_page(sm, after, PAGE)
        if not page:
            break
        after = page[-1].user_id
        for cand in page:
            due = due_reminders(cand.orders, now_ms)
            if not due:
                continue
            stats["users"] += 1
            try:
                claimed = await store.claim_reminders(sm, cand.user_id, [d.key for d in due])
                fresh = [d for d in due if d.key in claimed]
                stats["skipped_dup"] += len(due) - len(fresh)
                if not fresh:
                    continue
                try:
                    markup = build_start_keyboard(play_url, True)
                    await _send(bot, cand.tg_id, reminder_text(fresh), markup, sleep)
                except TelegramForbiddenError:  # blocked the bot: stop asking
                    stats["blocked"] += 1
                    await store.release_reminders(sm, cand.user_id, [d.key for d in fresh])
                    await store.set_opt_out(sm, cand.tg_id)
                    continue
                except (TelegramAPIError, OSError, asyncio.TimeoutError) as e:
                    stats["failed"] += 1
                    log.warning("reminder to user %s failed: %s: %s", cand.user_id, type(e).__name__, e)
                    await store.release_reminders(sm, cand.user_id, [d.key for d in fresh])
                    continue
                stats["sent"] += 1
                await sleep(SEND_INTERVAL_S)
            except Exception as e:  # DB trouble for this player: next run retries
                stats["failed"] += 1
                log.warning("reminders for user %s skipped: %s: %s", cand.user_id, type(e).__name__, e)
    return stats


async def order_reminders() -> dict[str, int] | None:
    """Scheduled job (every 5 minutes). Idle without BOT_TOKEN or DATABASE_URL."""
    rt = runtime.current()
    if rt is None or rt.bot is None:
        return None
    bot, settings, sm = rt.bot, rt.settings, rt.sm
    if sm is None:
        log.warning("order_reminders skipped: DATABASE_URL is not set")
        return None
    try:
        stats = await run_reminders(bot, sm, settings)
    except Exception as e:  # never take the scheduler down
        log.warning("order_reminders failed: %s: %s", type(e).__name__, e)
        return None
    if stats["sent"] or stats["failed"] or stats["blocked"]:
        log.info("order_reminders: %s", stats)
    return stats
