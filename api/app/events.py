"""Analytics events (S5): POST /api/events, a batch of up to 50 events from the web client's analytics.ts queue.

All-or-nothing validation: one bad event rejects the batch (nothing stored), the client drops or fixes it.
`source` is users.source (Telegram start_param). Demo bots never produce events (their batches are dropped).
Independent of the sim.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .bodies import invalid
from .errors import AppError
from .models import Event, User

log = logging.getLogger("mosprom.events")

MAX_BATCH = 50
MAX_FUTURE_S = 60  # client clocks drift a little; further ahead than this is rejected
MAX_PROPS_BYTES = 2048  # JSON size of one event's props
MAX_BODY_BYTES = 256 * 1024
TS_MIN_MS, TS_MAX_MS = 946_684_800_000, 4_102_444_800_000  # 2000-01-01 .. 2100-01-01 (a plain ms epoch)

# vendor/frontend/web/src/lib/analytics.ts `EventName` (drift is caught by tests/test_events.py when the vendor tree exists)
EVENT_NAMES = frozenset({
    "app_open", "age_confirmed", "session_start", "game_start", "game_end", "grade_up", "fact_unlock",
    "profession_card_view", "museum_open", "finale_view", "vacancy_click", "lead_form_open", "lead_submit",
    "share_click", "ref_join", "loan_take", "loan_repay", "loan_offer_shown",
})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)  # tests patch this


def parse_ts(raw: Any, i: int, now: datetime) -> datetime:
    """Client timestamp: ms since epoch (number) or ISO-8601 string (naive = UTC); absent = now."""
    if raw is None:
        return now
    if isinstance(raw, bool):
        raise invalid(f"events.{i}.ts", "ts must be a number of milliseconds or an ISO-8601 string")
    if isinstance(raw, (int, float)):
        if not TS_MIN_MS <= raw <= TS_MAX_MS:
            raise invalid(f"events.{i}.ts", "ts must be a plausible time in milliseconds since the epoch")
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            raise invalid(f"events.{i}.ts", "ts must be a number of milliseconds or an ISO-8601 string") from None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise invalid(f"events.{i}.ts", "ts must be a number of milliseconds or an ISO-8601 string")


def parse_batch(payload: Any, now: datetime) -> list[dict[str, Any]]:
    """`{events: [...]}` or a bare list -> rows (name, props, ts). Raises 422 too_many_events / invalid_event_name /
    future_timestamp / validation_error."""
    items = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise invalid("events", "Expected a list of events")
    if len(items) > MAX_BATCH:
        raise AppError("too_many_events", f"At most {MAX_BATCH} events per request",
                       details={"max": MAX_BATCH, "got": len(items)})
    rows = []
    for i, ev in enumerate(items):
        if not isinstance(ev, dict):
            raise invalid(f"events.{i}", "Expected an object {name, props?, ts}")
        name = ev.get("name")
        if not isinstance(name, str) or name not in EVENT_NAMES:
            raise AppError("invalid_event_name", "Unknown event name",
                           details={"index": i, "name": name if isinstance(name, str) and len(name) <= 64 else None})
        ts = parse_ts(ev.get("ts"), i, now)
        if ts > now + timedelta(seconds=MAX_FUTURE_S):
            raise AppError("future_timestamp", f"Event time is more than {MAX_FUTURE_S} s in the future",
                           details={"index": i, "name": name, "max_skew_s": MAX_FUTURE_S})
        props = ev.get("props")
        if props is None:
            props = {}
        elif not isinstance(props, dict):
            raise invalid(f"events.{i}.props", "props must be an object")
        if len(orjson.dumps(props)) > MAX_PROPS_BYTES:
            raise invalid(f"events.{i}.props", f"props is over {MAX_PROPS_BYTES} bytes", index=i)
        rows.append({"name": name, "props": props, "ts": ts})
    return rows


class EventService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None) -> None:
        if sessionmaker is None:
            raise AppError("config_error", "DATABASE_URL is not configured")
        self.sm = sessionmaker

    async def ingest(self, user: User, payload: Any) -> dict[str, int]:
        rows = parse_batch(payload, utcnow())
        if not rows or user.is_bot:
            return {"accepted": 0}
        async with self.sm() as session, session.begin():
            await session.execute(insert(Event), [{**r, "user_id": user.id, "source": user.source} for r in rows])
        return {"accepted": len(rows)}
