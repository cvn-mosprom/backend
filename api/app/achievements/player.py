"""Player-facing reads of the achievements (C2): GET /api/achievements/me, GET /api/achievements/catalog and the small blocks the
season summary / factory visit carry (`achievementPoints`, `topAchievements`).

What a player may see: the whole catalog with their own progress, what they earned, their points and their six competencies.
Never `player_scores.fit` (the fit to professions / vacancies is HR-only, docs/hr-crm-plan.md "Решения" 3). A hidden achievement
stays masked ("???": no title, description, target or competency) until the player has earned it.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..errors import AppError
from ..models import AchievementEarned, AchievementProgress, Factory, HrUser, PlayerScore, User
from . import catalog as cat

MASK_TITLE = "???"
MASK_TEXT = "Скрытое достижение: условие откроется после получения"
TOP_ACHIEVEMENTS = 3
_TIER_ORDER = {"gold": 3, "silver": 2, "bronze": 1}


def _ms(dt: Any) -> int:
    return int(dt.timestamp() * 1000) if dt else 0


def empty_competency() -> dict[str, int]:
    return {k: 0 for k in cat.COMPETENCIES}


def masked(entry: dict[str, Any]) -> dict[str, Any]:
    """A hidden, unearned catalog entry as a stranger sees it."""
    return {**entry, "title": MASK_TITLE, "description": MASK_TEXT, "competency": None, "target": None, "profession": None}


def short(a: cat.Achievement | str) -> dict[str, Any]:
    a = cat.BY_ID[a] if isinstance(a, str) else a
    return {"id": a.id, "title": a.title, "tier": a.tier, "points": a.points}


def top_achievements(rows: list[Any], limit: int = TOP_ACHIEVEMENTS) -> list[dict[str, Any]]:
    """The best `limit` earned achievements, one per id: most points first, then the higher tier, then the latest, then the id.
    Rows need `.achievement_id` and `.earned_at`; ids that left the catalog are skipped."""
    best: dict[str, Any] = {}
    for r in rows:
        if r.achievement_id in cat.BY_ID and (r.achievement_id not in best or r.earned_at > best[r.achievement_id].earned_at):
            best[r.achievement_id] = r
    ranked = sorted(best.values(), key=lambda r: (-cat.BY_ID[r.achievement_id].points,
                                                  -_TIER_ORDER[cat.BY_ID[r.achievement_id].tier], -_ms(r.earned_at),
                                                  r.achievement_id))
    return [short(r.achievement_id) for r in ranked[:limit]]


async def points_block(session: AsyncSession, user_id: int, season_id: int | None = None) -> dict[str, Any]:
    """{achievementPoints, seasonPoints, topAchievements} of a player from the earned rows (one query)."""
    rows = (await session.execute(select(AchievementEarned.achievement_id, AchievementEarned.points,
                                         AchievementEarned.earned_at, AchievementEarned.earned_season_id)
                                  .where(AchievementEarned.user_id == user_id))).all()
    return {"achievementPoints": sum(r.points for r in rows),
            "seasonPoints": sum(r.points for r in rows if season_id is not None and r.earned_season_id == season_id),
            "topAchievements": top_achievements(rows)}


async def my_achievements(sm: async_sessionmaker[AsyncSession], user: User) -> dict[str, Any]:
    async with sm() as session:
        season_id = (await session.execute(select(Factory.season_id).where(Factory.user_id == user.id))).scalar_one_or_none()
        earned = (await session.execute(select(AchievementEarned.achievement_id, AchievementEarned.season_key,
                                               AchievementEarned.points, AchievementEarned.earned_at,
                                               AchievementEarned.earned_season_id)
                                        .where(AchievementEarned.user_id == user.id)
                                        .order_by(AchievementEarned.earned_at.desc(), AchievementEarned.id.desc()))).all()
        progress = (await session.execute(select(AchievementProgress.achievement_id, AchievementProgress.season_key,
                                                 AchievementProgress.current, AchievementProgress.target)
                                          .where(AchievementProgress.user_id == user.id,
                                                 AchievementProgress.achievement_id != cat.COUNTERS_ID))).all()
        score = None
        if season_id is not None:
            score = (await session.execute(select(PlayerScore.competency).where(
                PlayerScore.user_id == user.id, PlayerScore.season_id == season_id))).scalar_one_or_none()
    got: dict[str, list[Any]] = {}
    for r in earned:
        got.setdefault(r.achievement_id, []).append(r)
    bar = {(r.achievement_id, r.season_key): r for r in progress}
    items: list[dict[str, Any]] = []
    for a in cat.CATALOG:
        rows = got.get(a.id, [])
        entry = a.public()
        entry["earned"] = bool(rows)
        entry["earnedAt"] = _ms(rows[0].earned_at) if rows else None  # rows are newest first
        entry["times"] = len(rows)
        p = None
        if a.progress:
            if rows and not a.seasonal:
                p = {"current": a.target, "target": a.target}
            else:
                row = bar.get((a.id, season_id if a.seasonal else 0))
                if row is not None:
                    p = {"current": min(row.current, row.target), "target": row.target}
        entry["progress"] = p
        items.append(masked(entry) | {"progress": None} if a.hidden and not rows else entry)
    comp = empty_competency()
    if isinstance(score, dict):
        comp.update({k: int(v) for k, v in score.items() if k in comp and isinstance(v, (int, float))})
    return {
        "catalogVersion": cat.CATALOG_VERSION, "seasonId": season_id,
        "achievementPoints": sum(r.points for r in earned),
        "seasonPoints": sum(r.points for r in earned if season_id is not None and r.earned_season_id == season_id),
        "earnedCount": len(got), "total": len(cat.CATALOG), "competency": comp, "items": items,
        "earned": [{**short(r.achievement_id), "earnedAt": _ms(r.earned_at), "seasonId": r.earned_season_id or None,
                    "seasonal": cat.BY_ID[r.achievement_id].seasonal} for r in earned if r.achievement_id in cat.BY_ID],
    }


async def catalog(sm: async_sessionmaker[AsyncSession], user: User) -> dict[str, Any]:
    """Short catalog for pickers (the CRM's vacancy requirements). HR sees every title; anybody else gets hidden ones masked."""
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    async with sm() as session:
        hr = (await session.execute(select(func.count()).select_from(HrUser).where(
            HrUser.tg_id == user.tg_id, HrUser.active.is_(True)))).scalar_one() > 0
    items = []
    for a in cat.CATALOG:
        items.append({"id": a.id, "title": MASK_TITLE if a.hidden and not hr else a.title, "tier": a.tier,
                      "points": a.points, "category": a.category, "competency": None if a.hidden and not hr else a.competency,
                      "hidden": a.hidden})
    return {"version": cat.CATALOG_VERSION, "items": items}
