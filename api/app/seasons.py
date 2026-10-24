"""Server-side season rotation (spec block 5, S4 part B).

Seasons are global and live in the `seasons` table. The rules mirror MockGameApi:
  * a season is closed when `now >= ends_at` (`closed=true`, `closed_at`), and its final standings are frozen into
    `season_results` (general ranking: stars desc, user id asc; bots only while BOTS_ENABLED);
  * the next season opens SEASON_ROLL_H hours after `ends_at` (the mock: `now - endsAt >= SEASON_ROLL_H * H`), starts
    at the moment it is opened and lasts SEASON_DAYS (vendor/frontend content/balance.ts);
  * `rotate()` is idempotent and race-safe (pg_advisory_xact_lock + re-check under the lock). It never locks factory
    rows (results come from a plain read), so it can run inside a factory write transaction without deadlocks.
    Factories are NOT bulk-updated: a factory moves to the new season lazily, when it is next loaded under its row lock
    (`align_state`), exactly when the mock would have rolled its own season.

Why the mock's own roll can never fight the server: MockGameApi.rollSeason fires inside bootstrap/advance when
`now - state.season.endsAt >= SEASON_ROLL_H h`. Before every sim call the API (game.GameService._lock) takes the
server's newest season (rotating it inline when it is overdue, see `current`) and rewrites `state.season.{id,startsAt,
endsAt}` to it. The state's season is then either open (`now < endsAt`) or closed for less than SEASON_ROLL_H hours
(otherwise the server season would have rolled first, with the same threshold), so the mock's condition is never true.
SEASON_ROLL_H is a constant on purpose: it must stay equal to the mock's.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Factory, Season, SeasonResult

log = logging.getLogger("mosprom.seasons")

SEASON_DAYS = 7  # SEASON_DAYS of vendor/frontend content/balance.ts
SEASON_ROLL_H = 1  # SEASON_ROLL_H of MockGameApi.ts: do not change without the mock
SEASON_LOCK_KEY = 0x4D50_5301  # pg_advisory_xact_lock: serialises close / open / bot growth / first season
DAY_MS = 86_400_000
ROLL_MS = SEASON_ROLL_H * 3_600_000
HEAVY_STATEMENT_TIMEOUT = "30s"  # rotation of a big table must outlive the 3 s default of request handlers


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def to_dt(t_ms: int) -> datetime:
    return datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)


@dataclass
class Rotation:
    closed: list[int] = field(default_factory=list)
    opened: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"closed": self.closed, "opened": self.opened}


async def lock(session: AsyncSession) -> None:
    """Transaction-scoped advisory lock shared by rotation, the first season and bot growth."""
    await session.execute(select(func.pg_advisory_xact_lock(SEASON_LOCK_KEY)))


async def newest(session: AsyncSession) -> Season | None:
    """The newest season (open, or the one that closed less than SEASON_ROLL_H h ago)."""
    return (await session.execute(
        select(Season).order_by(Season.id.desc()).limit(1).execution_options(populate_existing=True))).scalar_one_or_none()


def is_over(season: Season, now_ms: int) -> bool:
    return now_ms >= ms(season.ends_at)


def roll_overdue(season: Season, now_ms: int) -> bool:
    """The next season must exist already (the mock would roll its own now)."""
    return now_ms >= ms(season.ends_at) + ROLL_MS


async def _freeze(session: AsyncSession, season_id: int, bots_enabled: bool) -> None:
    place = func.row_number().over(order_by=(Factory.stars.desc(), Factory.user_id.asc()))
    sel = select(literal(season_id), Factory.user_id, place, Factory.stars, Factory.quality_avg, Factory.speed_bph,
                 Factory.bottles_season, Factory.is_bot).where(Factory.season_id == season_id)
    if not bots_enabled:  # hidden everywhere (rows, places, totals): the frozen table follows
        sel = sel.where(Factory.is_bot.is_(False))
    await session.execute(pg_insert(SeasonResult).from_select(
        ["season_id", "user_id", "place", "stars", "quality", "speed_bph", "bottles", "is_bot"], sel,
    ).on_conflict_do_nothing())


async def rotate(session: AsyncSession, now_ms: int, *, bots_enabled: bool = True, heavy: bool = False) -> Rotation:
    """Close every season whose `ends_at` passed (freezing `season_results`), then open the next one when the newest
    season ended >= SEASON_ROLL_H h ago. Idempotent: a second call right after returns an empty Rotation.
    Runs in the caller's transaction. `heavy=True` lifts the statement timeout for this transaction (bulk freeze)."""
    at = to_dt(now_ms)
    due = select(Season.id).where(Season.closed.is_(False), Season.ends_at <= at).order_by(Season.id)
    top = await newest(session)
    if top is None or not ((await session.execute(due.limit(1))).first() or (top.closed and roll_overdue(top, now_ms))):
        return Rotation()  # nothing to close, nothing to open: no lock taken (the common case of the 1 min job)
    if heavy:
        await session.execute(text(f"SET LOCAL statement_timeout = '{HEAVY_STATEMENT_TIMEOUT}'"))
    await lock(session)  # whoever got here first did the work: everything below re-reads the state
    out = Rotation()
    for sid in (await session.execute(due)).scalars().all():
        await session.execute(update(Season).where(Season.id == sid).values(closed=True, closed_at=at))
        await _freeze(session, sid, bots_enabled)
        out.closed.append(sid)
        log.info("season closed", extra={"fields": {"season_id": sid}})
    top = await newest(session)
    if top is not None and top.closed and roll_overdue(top, now_ms):
        out.opened = (await session.execute(pg_insert(Season).values(
            starts_at=at, ends_at=to_dt(now_ms + SEASON_DAYS * DAY_MS), days=SEASON_DAYS).returning(Season.id))).scalar_one()
        log.info("season opened", extra={"fields": {"season_id": out.opened, "previous": top.id}})
    return out


async def current(session: AsyncSession, now_ms: int, *, bots_enabled: bool = True) -> Season | None:
    """The season a factory write belongs to: the newest one (a closed one during the SEASON_ROLL_H h gap). When the
    rotation job is overdue (the newest season ended >= SEASON_ROLL_H h ago) it rotates inline first, so the state
    handed to the sim never lags behind the mock's own roll threshold. None only while there is no season at all."""
    season = await newest(session)
    if season is not None and roll_overdue(season, now_ms):
        await rotate(session, now_ms, bots_enabled=bots_enabled)
        season = await newest(session)
    return season


async def ensure(session: AsyncSession, now_ms: int, *, bots_enabled: bool = True) -> Season:
    """`current`, creating season 1 when the table is empty (first bootstrap ever). Race-safe."""
    season = await current(session, now_ms, bots_enabled=bots_enabled)
    if season is not None:
        return season
    await lock(session)
    season = await newest(session)  # created while we waited?
    if season is None:
        season = (await session.execute(pg_insert(Season).values(
            starts_at=to_dt(now_ms), ends_at=to_dt(now_ms + SEASON_DAYS * DAY_MS), days=SEASON_DAYS)
            .returning(Season))).scalar_one()
    return season


def align_state(state: dict[str, Any], season: Season) -> bool:
    """Make `state.season` (the mock's season block) the server season. When the id differs the mock's own season reset
    is applied (MockGameApi.rollSeason: stars = 0, shipped = {}, rejected = 0 in `state.sim`; layout, credits, xp,
    level, collection stay; `shiftsLeft` / `shiftsDay` are NOT touched). A matching id only syncs the bounds.
    Mutates `state`; returns True when anything changed. A state without a season block is left alone."""
    cur = state.get("season")
    if not isinstance(cur, dict):
        return False
    bounds = (season.id, ms(season.starts_at), ms(season.ends_at))
    if (cur.get("id"), cur.get("startsAt"), cur.get("endsAt")) == bounds:
        return False
    if cur.get("id") != season.id:
        sim = state.get("sim")
        if isinstance(sim, dict):
            sim["stars"] = 0
            sim["shipped"] = {}
            sim["rejected"] = 0
        log.info("factory moved to season", extra={"fields": {"from": cur.get("id"), "to": season.id}})
    cur["id"], cur["startsAt"], cur["endsAt"] = bounds
    return True


def patch_snapshot(snapshot: dict[str, Any], season: Season, now_ms: int) -> None:
    """Align `snapshot.season` of a freshly created factory (the sim built it with its own season 1 = now .. now + 7 d)
    without a second sim call: id / bounds from the server, `closed` and `dayIndex` as MockGameApi.snapshot computes them."""
    cur = snapshot.get("season")
    if not isinstance(cur, dict):
        return
    starts, ends = ms(season.starts_at), ms(season.ends_at)
    cur.update(id=season.id, startsAt=starts, endsAt=ends, closed=now_ms >= ends,
               dayIndex=max(1, min(SEASON_DAYS, (now_ms - starts) // DAY_MS + 1)), days=SEASON_DAYS)
