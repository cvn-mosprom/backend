"""Analytics events (S5): POST /api/events, a batch of up to 50 events from the web client's analytics.ts queue.

Partial accept (S13): known, valid events are stored; unknown names, bad timestamps (future included) and bad props are
dropped and counted in `rejected` -- the web client throws a batch away on any 4xx, so one bad item must not cost the rest.
Still a 422 for the whole request: a malformed envelope (validation_error) and more than 50 events (too_many_events).
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


class Bad(Exception):
    """One event of the batch is unusable: it is dropped (reported in `rejected`), the rest of the batch is kept."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_ts(raw: Any, now: datetime) -> datetime:
    """Client timestamp: ms since epoch (number) or ISO-8601 string (naive = UTC); absent = now. Raises Bad('invalid_ts')."""
    if raw is None:
        return now
    if isinstance(raw, bool):
        raise Bad("invalid_ts")
    if isinstance(raw, (int, float)):
        if not TS_MIN_MS <= raw <= TS_MAX_MS:
            raise Bad("invalid_ts")
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            raise Bad("invalid_ts") from None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise Bad("invalid_ts")


def parse_event(ev: Any, now: datetime) -> dict[str, Any]:
    """One event -> row (name, props, ts). Raises Bad(reason): invalid_event | invalid_event_name | invalid_ts |
    future_timestamp | invalid_props | props_too_large."""
    if not isinstance(ev, dict):
        raise Bad("invalid_event")
    name = ev.get("name")
    if not isinstance(name, str) or name not in EVENT_NAMES:
        raise Bad("invalid_event_name")
    ts = parse_ts(ev.get("ts"), now)
    if ts > now + timedelta(seconds=MAX_FUTURE_S):
        raise Bad("future_timestamp")
    props = ev.get("props")
    if props is None:
        props = {}
    elif not isinstance(props, dict):
        raise Bad("invalid_props")
    try:
        too_big = len(orjson.dumps(props)) > MAX_PROPS_BYTES
    except TypeError:
        raise Bad("invalid_props") from None
    if too_big:
        raise Bad("props_too_large")
    return {"name": name, "props": props, "ts": ts}


def parse_batch(payload: Any, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`{events: [...]}` or a bare list -> (rows to store, rejected [{index, reason, name?}]).
    Only a malformed envelope (422 validation_error) or too many events (422 too_many_events) fails the whole request:
    a single bad event never throws away the rest of a client batch."""
    items = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise invalid("events", "Expected a list of events")
    if len(items) > MAX_BATCH:
        raise AppError("too_many_events", f"At most {MAX_BATCH} events per request",
                       details={"max": MAX_BATCH, "got": len(items)})
    rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for i, ev in enumerate(items):
        try:
            rows.append(parse_event(ev, now))
        except Bad as e:
            item: dict[str, Any] = {"index": i, "reason": e.reason}
            name = ev.get("name") if isinstance(ev, dict) else None
            if isinstance(name, str) and len(name) <= 64:
                item["name"] = name
            rejected.append(item)
    return rows, rejected


class EventService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None) -> None:
        if sessionmaker is None:
            raise AppError("config_error", "DATABASE_URL is not configured")
        self.sm = sessionmaker

    async def ingest(self, user: User, payload: Any) -> dict[str, Any]:
        """-> {accepted, rejected, rejects?}: `rejected` = number of dropped events, `rejects` = [{index, reason, name?}]."""
        rows, rejected = parse_batch(payload, utcnow())
        out: dict[str, Any] = {"accepted": 0, "rejected": len(rejected)}
        if rejected:
            out["rejects"] = rejected
        if not rows or user.is_bot:
            return out
        async with self.sm() as session, session.begin():
            await session.execute(insert(Event), [{**r, "user_id": user.id, "source": user.source} for r in rows])
        out["accepted"] = len(rows)
        return out
