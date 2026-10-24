"""SQL of the player features. The bot writes only ``users.bot_started_at`` / ``notify_*`` and ``reminders_sent``; the factories
table is read-only here and only its ``snapshot -> orders`` part (never ``state``).

A player who presses /start before ever opening the Mini App has no ``users`` row yet: ``register_start`` creates a minimal
one (tg_id, names, source = the deep-link start parameter, bot_started_at). The api's login later finds the row by tg_id, refreshes
username / names and keeps the source (first touch wins) - so nothing is lost either way. Such a row has no factory: analytics
that count *players* must join ``factories`` (or look at ``bot_started_at``), not count ``users``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

SessionMaker = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class Profile:
    tg_id: int
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    source: str | None = None  # validated /start parameter


async def register_start(sm: SessionMaker, p: Profile) -> bool:
    """First /start: create or touch the user row, remember when the bot was started; returns the current opt-in."""
    async with sm() as s, s.begin():
        row = (await s.execute(text(
            "INSERT INTO users (tg_id, username, first_name, last_name, source, bot_started_at) "
            "VALUES (:tg, :un, :fn, :ln, :src, now()) "
            "ON CONFLICT (tg_id) DO UPDATE SET bot_started_at = COALESCE(users.bot_started_at, now()), "
            "source = COALESCE(users.source, EXCLUDED.source) "
            "RETURNING notify_opt_in"), {"tg": p.tg_id, "un": p.username, "fn": p.first_name, "ln": p.last_name,
                                         "src": p.source})).one()
    return bool(row.notify_opt_in)


async def set_opt_in(sm: SessionMaker, p: Profile) -> bool:
    """Reminders on. True when the state changed (opt_in_at is the moment of the change, repeated taps keep it)."""
    async with sm() as s, s.begin():
        row = (await s.execute(text(
            "WITH prev AS (SELECT notify_opt_in FROM users WHERE tg_id = :tg) "
            "INSERT INTO users (tg_id, username, first_name, last_name, bot_started_at, notify_opt_in, notify_opt_in_at) "
            "VALUES (:tg, :un, :fn, :ln, now(), true, now()) "
            "ON CONFLICT (tg_id) DO UPDATE SET bot_started_at = COALESCE(users.bot_started_at, now()), "
            "notify_opt_in_at = CASE WHEN users.notify_opt_in THEN users.notify_opt_in_at ELSE now() END, "
            "notify_opt_in = true "
            "RETURNING (SELECT notify_opt_in FROM prev) AS was_on"),
            {"tg": p.tg_id, "un": p.username, "fn": p.first_name, "ln": p.last_name})).one()
    return row.was_on is not True  # no row before, or it was off


async def set_opt_out(sm: SessionMaker, tg_id: int) -> bool:
    """Reminders off. True when they were on (a user who never opted in is left alone)."""
    async with sm() as s, s.begin():
        row = (await s.execute(text(
            "UPDATE users SET notify_opt_in = false, notify_opt_out_at = now() "
            "WHERE tg_id = :tg AND notify_opt_in RETURNING id"), {"tg": tg_id})).first()
    return row is not None


@dataclass(frozen=True)
class Candidate:
    user_id: int
    tg_id: int
    orders: Any  # factories.snapshot -> 'orders' (a JSON array; anything else is tolerated downstream)


async def opted_in_page(sm: SessionMaker, after_id: int = 0, limit: int = 200) -> list[Candidate]:
    """Opted-in, bot-started, real (non is_bot) players that have a stored snapshot with an orders array, keyset-paged by user id."""
    async with sm() as s:
        rows = (await s.execute(text(
            "SELECT u.id, u.tg_id, f.snapshot -> 'orders' AS orders "
            "FROM users u JOIN factories f ON f.user_id = u.id "
            "WHERE u.id > :after AND u.notify_opt_in AND u.bot_started_at IS NOT NULL "
            "AND NOT u.is_bot AND u.tg_id > 0 AND NOT f.is_bot "
            "AND f.snapshot IS NOT NULL AND jsonb_typeof(f.snapshot -> 'orders') = 'array' "
            "ORDER BY u.id LIMIT :lim"), {"after": after_id, "lim": limit})).all()
    out = []
    for r in rows:
        orders = r.orders
        if isinstance(orders, (str, bytes)):  # driver without a JSONB codec
            try:
                orders = json.loads(orders)
            except ValueError:
                orders = None
        out.append(Candidate(int(r.id), int(r.tg_id), orders))
    return out


def _arrays(items: Iterable[tuple[str, str]]) -> tuple[list[str], list[str]]:
    pairs = list(items)
    return [o for o, _ in pairs], [k for _, k in pairs]


async def claim_reminders(sm: SessionMaker, user_id: int, items: Sequence[tuple[str, str]]) -> set[tuple[str, str]]:
    """Reserve (order_id, kind) pairs before sending: the returned subset was NOT reminded yet (at-most-once)."""
    if not items:
        return set()
    orders, kinds = _arrays(items)
    async with sm() as s, s.begin():
        rows = (await s.execute(text(
            "INSERT INTO reminders_sent (user_id, order_id, kind) "
            "SELECT :uid, t.o, t.k FROM unnest(CAST(:orders AS text[]), CAST(:kinds AS text[])) AS t(o, k) "
            "ON CONFLICT DO NOTHING RETURNING order_id, kind"), {"uid": user_id, "orders": orders, "kinds": kinds})).all()
    return {(r.order_id, r.kind) for r in rows}


async def release_reminders(sm: SessionMaker, user_id: int, items: Sequence[tuple[str, str]]) -> None:
    """Undo a claim after a failed send, so the next run tries again."""
    if not items:
        return
    orders, kinds = _arrays(items)
    async with sm() as s, s.begin():
        await s.execute(text(
            "DELETE FROM reminders_sent WHERE user_id = :uid AND (order_id, kind) IN "
            "(SELECT t.o, t.k FROM unnest(CAST(:orders AS text[]), CAST(:kinds AS text[])) AS t(o, k))"),
            {"uid": user_id, "orders": orders, "kinds": kinds})
