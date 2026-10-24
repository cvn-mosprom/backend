"""C5: HR analytics, `GET /api/hr/analytics?period=7d|30d|all` (the web client's `Analytics`).

The same rules and SQL ideas as the bot's `/funnel /sources /stats` (`bot/app/hr/queries.py`; the bot is a separate image and cannot
be imported from here, so the queries are re-stated, not shared): demo bots (`users.is_bot`) and test users (`users.is_test`) never
count, a lead whose consent was revoked never counts. Candidates = leads, including the ones of the public landing (no player row).

`period` is a window on the creation time: game events by `events.ts`, players by `users.created_at`, leads (and everything derived
from them: statuses, sources, universities, points, competencies) by `leads.created_at`. `all` has no lower bound.

Not per role: nothing here is personal data, so the 60 s Redis cache is shared by every HR role.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..achievements.catalog import COMPETENCIES

log = logging.getLogger("mosprom.hr")

PERIODS: dict[str, timedelta | None] = {"7d": timedelta(days=7), "30d": timedelta(days=30), "all": None}
CACHE_TTL_S = 60
CACHE_PREFIX = "hr:analytics:"

STATUSES = ("new", "screening", "interview", "offer", "hired", "rejected", "reserve")
STATUS_ALIASES = {"contacted": "screening"}  # the pre-C4 name, in case a row is read before the migration
# game events that mark progress before the form (distinct players); the rest of the funnel comes from the leads table
EVENT_STAGES: tuple[tuple[str, str], ...] = (
    ("app_open", "Открыли игру"),
    ("session_start", "Начали сессию (интро)"),
    ("game_start", "Запустили смену"),
    ("game_end", "Завершили смену"),
    ("finale_view", "Дошли до финала сезона"),
    ("lead_form_open", "Открыли анкету"),
)
NO_SOURCE = "напрямую"
BUCKET_WIDTH, BUCKETS = 100, 8  # 0-99 ... 600-699, the last one is open-ended (>= 700)
MAX_SOURCES, MAX_UNIVERSITIES = 30, 50

_FROM = "FROM leads l LEFT JOIN users u ON u.id = l.user_id"
_WHERE = ("WHERE coalesce(u.is_bot, false) = false AND coalesce(u.is_test, false) = false AND l.consent_revoked_at IS NULL "
          "AND (CAST(:since AS timestamptz) IS NULL OR l.created_at >= :since)")
_LEADS = f"{_FROM} {_WHERE}"
# candidates with a player row, plus the score row of the player's current season (the one the C4 list shows)
_SCORED = (f"{_FROM} LEFT JOIN LATERAL (SELECT s.achievement_points, s.competency FROM factories f "
           f"JOIN player_scores s ON s.user_id = f.user_id AND s.season_id = f.season_id WHERE f.user_id = l.user_id) ps ON true "
           f"{_WHERE} AND l.user_id IS NOT NULL")


def _status(s: str) -> str:
    return STATUS_ALIASES.get(s, s)


async def compute(session: AsyncSession, period: str, now: datetime | None = None) -> dict[str, Any]:
    delta = PERIODS[period]
    since = (now or datetime.now(timezone.utc)) - delta if delta is not None else None
    p = {"since": since}

    # --- statuses
    statuses = dict.fromkeys(STATUSES, 0)
    for r in (await session.execute(text(f"SELECT l.status, count(*) AS n {_LEADS} GROUP BY l.status"), p)).all():
        if _status(r.status) in statuses:
            statuses[_status(r.status)] += int(r.n)
    leads = sum(statuses.values())

    # --- funnel: distinct players per game event, then the leads table by status
    ev = (await session.execute(text(
        "SELECT e.name, count(DISTINCT e.user_id) AS n FROM events e JOIN users u ON u.id = e.user_id "
        "WHERE u.is_bot = false AND u.is_test = false AND e.name = ANY(CAST(:names AS text[])) "
        "AND (CAST(:since AS timestamptz) IS NULL OR e.ts >= :since) GROUP BY e.name"),
        {**p, "names": [n for n, _ in EVENT_STAGES]})).all()
    by_event = {r.name: int(r.n) for r in ev}
    funnel = [{"stage": n, "label": label, "count": by_event.get(n, 0)} for n, label in EVENT_STAGES]
    funnel += [
        {"stage": "lead", "label": "Оставили анкету", "count": leads},
        {"stage": "interview", "label": "Дошли до интервью", "count": sum(statuses[s] for s in ("interview", "offer", "hired"))},
        {"stage": "offer", "label": "Получили оффер", "count": statuses["offer"] + statuses["hired"]},
        {"stage": "hired", "label": "Приняты", "count": statuses["hired"]},
    ]

    # --- sources: players (first seen in the period) -> leads -> hired
    src: dict[str, dict[str, int]] = {}
    for r in (await session.execute(text(
            "SELECT coalesce(nullif(u.source, ''), :none) AS source, count(*) AS n FROM users u "
            "WHERE u.is_bot = false AND u.is_test = false AND (CAST(:since AS timestamptz) IS NULL OR u.created_at >= :since) "
            "AND NOT EXISTS (SELECT 1 FROM hr_users h WHERE h.tg_id = u.tg_id) "  # HR staff logging into the CRM are not players
            "GROUP BY 1"), {**p, "none": NO_SOURCE})).all():
        src.setdefault(r.source, {"players": 0, "leads": 0, "hired": 0})["players"] = int(r.n)
    for r in (await session.execute(text(
            f"SELECT coalesce(nullif(l.source, ''), nullif(u.source, ''), :none) AS source, count(*) AS n, "
            f"count(*) FILTER (WHERE l.status = 'hired') AS hired {_LEADS} GROUP BY 1"), {**p, "none": NO_SOURCE})).all():
        e = src.setdefault(r.source, {"players": 0, "leads": 0, "hired": 0})
        e["leads"], e["hired"] = int(r.n), int(r.hired)
    sources = [{"source": k, **v} for k, v in src.items()]
    sources.sort(key=lambda s: (-s["leads"], -s["players"], s["source"]))

    # --- universities
    universities = [
        {"name": r.name, "leads": int(r.n), "interview": int(r.interview), "hired": int(r.hired)}
        for r in (await session.execute(text(
            f"SELECT l.university AS name, count(*) AS n, count(*) FILTER (WHERE l.status IN ('interview', 'offer', 'hired')) AS interview, "
            f"count(*) FILTER (WHERE l.status = 'hired') AS hired {_LEADS} GROUP BY l.university "
            f"ORDER BY n DESC, l.university LIMIT {MAX_UNIVERSITIES}"), p)).all()]

    # --- achievement points and competencies: candidates with a player row (landing-only leads have no game profile)
    buckets = [0] * BUCKETS
    for r in (await session.execute(text(
            f"SELECT least(coalesce(ps.achievement_points, 0) / {BUCKET_WIDTH}, {BUCKETS - 1}) AS b, count(*) AS n "
            f"{_SCORED} GROUP BY 1"), p)).all():
        buckets[int(r.b)] += int(r.n)
    points = [{"from": i * BUCKET_WIDTH, "to": i * BUCKET_WIDTH + BUCKET_WIDTH - 1, "count": n} for i, n in enumerate(buckets)]
    avg_cols = ", ".join(f"avg((ps.competency ->> '{k}')::numeric) AS {k}" for k in COMPETENCIES)
    avg = (await session.execute(text(f"SELECT {avg_cols} {_SCORED}"), p)).mappings().one()
    competency_avg = {k: int(round(float(avg[k]))) if avg[k] is not None else 0 for k in COMPETENCIES}

    return {"period": period, "funnel": funnel, "statuses": statuses, "sources": sources[:MAX_SOURCES], "points": points,
            "competencyAvg": competency_avg, "universities": universities}


async def cached(app: Any, session_factory: Any, period: str) -> dict[str, Any]:
    """`compute` behind a 60 s Redis cache (shared by all roles). Redis trouble never fails the request."""
    redis, key = app.state.redis, CACHE_PREFIX + period
    if redis is not None:
        try:
            raw = await redis.get(key)
            if raw:
                return orjson.loads(raw)
        except Exception as e:
            log.warning("hr analytics cache read failed: %s", type(e).__name__)
    async with session_factory() as session:
        data = await compute(session, period)
    if redis is not None:
        try:
            await redis.set(key, orjson.dumps(data).decode(), ex=CACHE_TTL_S)
        except Exception as e:
            log.warning("hr analytics cache write failed: %s", type(e).__name__)
    return data
