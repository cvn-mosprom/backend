"""Leaderboard, public factory view and season summary (S4). Shapes mirror `Leaderboard` / `SeasonSummary` /
`GameApi.visit` of vendor/frontend/web/src/lib/api/types.ts (MockGameApi.board / visit / seasonSummary).

Everything is read from the denormalised `factories` columns (stars, quality_avg, speed_bph, university, is_bot), never
from the sim:
  * the top-100 of a (season, tab, university) board is cached in Redis for `leaderboard_cache_ttl_s` (15-30 s, user
    agnostic: `isMe` and the `me` block are added per request); a Redis failure fails open (the board is computed);
  * the `me` block (place, value, gapTo51, inviteCutoff) is computed per request: one rank query over the same universe,
    ties broken by user id (ascending), the same order the rows come in;
  * `university` / `university_id` filter = the exact university NAME (there is no university table: the frontend's
    UNIVERSITIES list is the registry); with a filter the universe, places and `me` are restricted to that university,
    and `me` is omitted for a player of another university;
  * C2: the `achievements` tab ranks by achievement points (player_scores of the board's season; `scope=season` = the part earned in
    that season). Only players with points > 0 are on it, so demo bots never are and `me` is absent for a player with none;
  * demo bots (is_bot) are part of the universe unless `bots_enabled` is false (BOTS_ENABLED=0), then they are invisible
    everywhere: rows, places, totals, aggregates, visit.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import orjson
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from .achievements import player as ach_player
from .errors import AppError
from .models import Factory, PlayerScore, Season, User

log = logging.getLogger("mosprom.leaderboard")

TABS = ("general", "quality", "speed", "uni", "achievements")
SCOPES = ("all", "season")  # achievements tab: lifetime points | points earned in the season
BOARD_SIZE = 100
INVITE_PLACES = 50  # places 1..50 are the invitation zone; inviteCutoff = value at place 50
UNIVERSITIES_MAX = 50
VERBS = {  # content/professions.ts `verb` (season summary: best-profession reason)
    "kvasovar": "СЛЕДИ ЗА ЧАНОМ!", "operator": "ТАПНИ ВОВРЕМЯ!", "adjuster": "ВЫТАЩИ!", "lab": "НАЙДИ БРАК!",
    "storekeeper": "СЛОЖИ!", "forklift": "ОБЪЕЗЖАЙ!", "kipia": "СОЕДИНИ!",
}
BEST_FALLBACK = ("operator", "ты ещё не выходил на смену — начни с оператора линии")


def display_name(user: User) -> str:
    """Name shown to other players (same rule as the player name the sim gets on factory creation)."""
    return " ".join(p for p in (user.first_name, user.last_name) if p) or user.username or f"Player {user.tg_id}"


def column(tab: str):
    """Ranking column of a tab (`uni` ranks players by stars; its extra block is the university aggregate)."""
    return {"quality": Factory.quality_avg, "speed": Factory.speed_bph}.get(tab, Factory.stars)


def shown(tab: str, raw: float | int | None) -> float | int:
    """Value on the wire: stars / bottles per hour are integers, quality is a percentage with one decimal."""
    raw = raw or 0
    return round(raw * 1000) / 10 if tab == "quality" else int(raw)


def _ms(dt: datetime | None) -> int:
    return int(dt.timestamp() * 1000) if dt else 0


def points_column(scope: str, ps=PlayerScore):
    """Points of the achievements tab (0 for a player without a player_scores row)."""
    return func.coalesce(ps.season_points if scope == "season" else ps.achievement_points, 0)


def _with_scores(q, f=Factory, ps=PlayerScore):
    """LEFT JOIN player_scores of the factory's season (the factory row is in `q`'s FROM)."""
    return q.outerjoin(ps, and_(ps.user_id == f.user_id, ps.season_id == f.season_id))


def _universe(q, season_id: int, bots_enabled: bool, university: str | None, f=Factory):
    q = q.where(f.season_id == season_id, f.is_test.is_(False))  # e2e / test players are never ranked (S13)
    if not bots_enabled:
        q = q.where(f.is_bot.is_(False))
    if university:
        q = q.where(f.university == university)
    return q


async def rank_of(session: AsyncSession, season_id: int, user_id: int, tab: str = "general", *,
                  bots_enabled: bool = True, university: str | None = None,
                  mine: int | float | None = None, scope: str = "all") -> tuple[int, int]:
    """(place, total) of `user_id` in the universe: 1 + players strictly ahead (higher value, or equal value and lower
    user id). `mine` = the player's own value (default: read from the player's row, which must exist)."""
    other = aliased(Factory)
    if tab == "achievements":  # universe = players with points; ties by user id like every tab
        ps = aliased(PlayerScore)
        ocol = points_column(scope, ps)
        if mine is None:
            mine = (await session.execute(select(points_column(scope)).where(
                PlayerScore.user_id == user_id, PlayerScore.season_id == season_id))).scalar_one_or_none() or 0
        q = (select(func.count().filter(or_(ocol > mine, and_(ocol == mine, other.user_id < user_id))).label("ahead"),
                    func.count().label("n")).select_from(other)
             .outerjoin(ps, and_(ps.user_id == other.user_id, ps.season_id == other.season_id))
             .where(other.user_id != user_id, ocol > 0))
        row = (await session.execute(_universe(q, season_id, bots_enabled, university, other))).one()
        return row.ahead + 1, row.n + 1
    col = column(tab)
    own = select(col).where(Factory.user_id == user_id).scalar_subquery() if mine is None else mine
    ocol = {"quality": other.quality_avg, "speed": other.speed_bph}.get(tab, other.stars)
    q = select(
        func.count().filter(or_(ocol > own, and_(ocol == own, other.user_id < user_id))).label("ahead"),
        func.count().label("n"),
    ).where(other.user_id != user_id)
    row = (await session.execute(_universe(q, season_id, bots_enabled, university, other))).one()
    return row.ahead + 1, row.n + 1


class LeaderboardService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None, redis: Any, *, bots_enabled: bool = True,
                 cache_ttl_s: int = 20) -> None:
        if sessionmaker is None:
            raise AppError("config_error", "DATABASE_URL is not configured")
        self.sm = sessionmaker
        self.redis = redis
        self.bots_enabled = bots_enabled
        self.ttl = cache_ttl_s

    # ---------------------------------------------------------------- GET /api/season/leaderboard

    async def leaderboard(self, user: User, tab: str, limit: int, university: str | None,
                          season_id: int | None, scope: str = "all") -> dict[str, Any]:
        if tab not in TABS:
            raise AppError("invalid_tab", f"Unknown tab {tab!r}", details={"tab": tab, "allowed": list(TABS)})
        if scope not in SCOPES:
            raise AppError("invalid_scope", f"Unknown scope {scope!r}", details={"scope": scope, "allowed": list(SCOPES)})
        async with self.sm() as session:
            sid = await self._season(session, season_id)
            board = await self._board(session, sid, tab, university, scope)
            me = await self._me(session, user, sid, tab, university, scope)
        pid = str(user.id)
        rows = [{**r, **({"isMe": True} if r["playerId"] == pid else {})} for r in board["rows"][:limit]]
        top = board["rows"]
        cutoff = top[INVITE_PLACES - 1]["value"] if len(top) >= INVITE_PLACES else 0
        out: dict[str, Any] = {"tab": tab, "seasonId": sid, "rows": rows, "total": board["total"], "inviteCutoff": cutoff}
        if me is not None:
            place = me.pop("_place")
            out["total"] = max(me.pop("_total"), place)
            next_value = top[INVITE_PLACES]["value"] if len(top) > INVITE_PLACES else 0
            gap = me["value"] - next_value if place <= INVITE_PLACES else cutoff - me["value"]
            out["me"] = me
            out["gapTo51"] = max(0, round(gap, 1) if tab == "quality" else int(gap))
        if board.get("universities") is not None:
            out["universities"] = board["universities"]
        return out

    async def _season(self, session: AsyncSession, season_id: int | None) -> int:
        """The requested season, else the newest open one (newest of all when none is open)."""
        if season_id is not None:
            if (await session.execute(select(Season.id).where(Season.id == season_id))).scalar_one_or_none() is None:
                raise AppError("season_not_found", "Season not found", details={"season_id": season_id})
            return season_id
        q = select(Season.id).order_by(Season.closed.asc(), Season.id.desc()).limit(1)
        sid = (await session.execute(q)).scalar_one_or_none()
        if sid is None:
            raise AppError("season_not_found", "No season yet, call GET /api/bootstrap first")
        return sid

    async def _board(self, session: AsyncSession, season_id: int, tab: str, university: str | None,
                     scope: str = "all") -> dict[str, Any]:
        key = f"lb:{season_id}:{tab}:{university or ''}:{int(self.bots_enabled)}" + (f":{scope}" if tab == "achievements" else "")
        try:
            raw = await self.redis.get(key) if self.redis is not None else None
            cached = orjson.loads(raw) if raw else None
        except Exception as e:  # fail open: compute from the database
            log.warning("leaderboard cache read failed: %s", type(e).__name__)
            cached = None
        if isinstance(cached, dict) and "rows" in cached:
            return cached
        board = await self._compute(session, season_id, tab, university, scope)
        try:
            if self.redis is not None:
                await self.redis.set(key, orjson.dumps(board).decode(), ex=self.ttl)
        except Exception as e:
            log.warning("leaderboard cache write failed: %s", type(e).__name__)
        return board

    async def _compute(self, session: AsyncSession, season_id: int, tab: str, university: str | None,
                       scope: str = "all") -> dict[str, Any]:
        col = points_column(scope) if tab == "achievements" else column(tab)
        q = (select(Factory.user_id, Factory.name, Factory.university, col.label("raw"),
                    User.first_name, User.last_name, User.username, User.tg_id)
             .join(User, User.id == Factory.user_id)
             .order_by(col.desc(), Factory.user_id).limit(BOARD_SIZE))
        if tab == "achievements":
            q = _with_scores(q).where(col > 0)
        rows = []
        for i, r in enumerate((await session.execute(_universe(q, season_id, self.bots_enabled, university))).all(), 1):
            row: dict[str, Any] = {
                "place": i, "playerId": str(r.user_id),
                "name": " ".join(p for p in (r.first_name, r.last_name) if p) or r.username or f"Player {r.tg_id}",
                "factoryName": r.name, "value": shown(tab, r.raw)}
            if r.university:
                row["university"] = r.university
            rows.append(row)
        count_q = select(func.count()).select_from(Factory)
        if tab == "achievements":
            count_q = _with_scores(count_q).where(col > 0)
        total = (await session.execute(_universe(count_q, season_id, self.bots_enabled, university))).scalar_one()
        board: dict[str, Any] = {"rows": rows, "total": total}
        if tab == "uni":
            total_stars = func.sum(Factory.stars)
            uq = (select(Factory.university, total_stars).where(Factory.university.is_not(None), Factory.university != "")
                  .group_by(Factory.university).order_by(total_stars.desc(), Factory.university).limit(UNIVERSITIES_MAX))
            board["universities"] = [{"name": n, "value": int(v)}
                                     for n, v in (await session.execute(_universe(uq, season_id, self.bots_enabled, None))).all()]
        return board

    async def _me(self, session: AsyncSession, user: User, season_id: int, tab: str,
                  university: str | None, scope: str = "all") -> dict[str, Any] | None:
        col = points_column(scope) if tab == "achievements" else column(tab)
        q = select(Factory.season_id, Factory.name, Factory.university, col.label("raw")).where(Factory.user_id == user.id)
        mine = (await session.execute(_with_scores(q) if tab == "achievements" else q)).one_or_none()
        if mine is None or mine.season_id != season_id or (university and mine.university != university):
            return None
        if tab == "achievements" and not mine.raw:  # no points: not on the board
            return None
        place, total = await rank_of(session, season_id, user.id, tab, bots_enabled=self.bots_enabled,
                                     university=university, mine=mine.raw, scope=scope)
        row: dict[str, Any] = {"place": place, "playerId": str(user.id), "name": display_name(user),
                               "factoryName": mine.name, "value": shown(tab, mine.raw), "isMe": True,
                               "_place": place, "_total": total}
        if mine.university:
            row["university"] = mine.university
        return row

    # ---------------------------------------------------------------- GET /api/factory/{playerId}

    async def visit(self, player_id: str) -> dict[str, Any]:
        """Public cut of somebody's factory: no credits / loan / stock / history (nor xp, orders, skills)."""
        not_found = AppError("player_not_found", "Player not found", details={"player_id": player_id})
        if not (player_id.isascii() and player_id.isdigit()) or len(player_id) > 19 or int(player_id) >= 2**63:  # int() caps at 4300 digits
            raise not_found
        layout = func.coalesce(Factory.state[("sim", "layout")], Factory.snapshot[("factory", "layout")], type_=JSONB)
        q = (select(User.id, User.first_name, User.last_name, User.username, User.tg_id, User.university, User.is_bot,
                    Factory.name, Factory.level, Factory.stars, Factory.university.label("f_uni"),
                    Factory.updated_at, layout.label("layout"))
             .join(Factory, Factory.user_id == User.id).where(User.id == int(player_id)))
        async with self.sm() as session:
            r = (await session.execute(q)).one_or_none()
            ach = await ach_player.points_block(session, r.id) if r is not None else {}
        if r is None or (r.is_bot and not self.bots_enabled) or not isinstance(r.layout, dict):
            raise not_found
        player: dict[str, Any] = {"id": str(r.id), "name": " ".join(p for p in (r.first_name, r.last_name) if p)
                                  or r.username or f"Player {r.tg_id}"}
        if r.f_uni or r.university:
            player["university"] = r.f_uni or r.university
        return {"player": player, "factory": {"name": r.name, "level": r.level, "stars": r.stars,
                                              "layout": r.layout, "updatedAt": _ms(r.updated_at)},
                "achievementPoints": ach["achievementPoints"], "topAchievements": ach["topAchievements"]}  # C2

    # ---------------------------------------------------------------- GET /api/season/summary

    async def summary(self, user: User) -> dict[str, Any]:
        async with self.sm() as session:
            f = (await session.execute(select(Factory.season_id, Factory.credits, Factory.stars, Factory.level,
                                              Factory.quality_avg, Factory.snapshot)
                                       .where(Factory.user_id == user.id))).one_or_none()
            if f is None:
                raise AppError("factory_not_found", "Factory not found, call GET /api/bootstrap first")
            place, total = await rank_of(session, f.season_id, user.id, bots_enabled=self.bots_enabled, mine=f.stars)
            from .leads import lead_status  # S5 (local: leads imports this module)
            from .quiz import quiz_result
            quiz, lead = await quiz_result(session, user.id), await lead_status(session, user.id)
            ach = await ach_player.points_block(session, user.id, f.season_id)
        snap = f.snapshot if isinstance(f.snapshot, dict) else {}
        fac = snap.get("factory") if isinstance(snap.get("factory"), dict) else {}
        layout = fac.get("layout") if isinstance(fac.get("layout"), dict) else {}
        buildings = layout.get("buildings") if isinstance(layout.get("buildings"), list) else []
        collection = fac.get("collection") if isinstance(fac.get("collection"), dict) else {}
        produced = fac.get("produced")
        best, reason = best_profession(snap.get("skills"))
        return {
            "place": place, "total": total, "inInviteZone": place <= INVITE_PLACES,
            "bottles": int(produced) if isinstance(produced, (int, float)) and not isinstance(produced, bool) else 0,
            "quality": float(f.quality_avg), "lines": sum(1 for b in buildings if isinstance(b, dict) and b.get("kind") == "packer"),
            "skus": len(collection), "bestProfession": best, "bestProfessionReason": reason,
            # beyond the frontend type (spec: credits, stars, level, quiz result, lead status)
            "seasonId": f.season_id, "credits": f.credits, "stars": f.stars, "level": f.level,
            "quizResult": quiz,  # {correct, total, score 0..100, answered, completed, bankTotal} (quiz.quiz_progress) or null
            "leadStatus": lead,  # status of the latest lead, 'revoked' after a consent revocation, or null (S5)
            # C2: achievement points (lifetime, and the part earned in this season) and the 3 best earned achievements
            "achievementPoints": ach["achievementPoints"], "seasonPoints": ach["seasonPoints"],
            "topAchievements": ach["topAchievements"],
        }


def best_profession(skills: Any) -> tuple[str, str]:
    """MockGameApi.seasonSummary: best = max(win-rate*0.6 + avgScore*0.4 + min(played, 10)*0.01)."""
    by = skills.get("byProfession") if isinstance(skills, dict) else None
    best, best_score = None, -1.0
    for pid, v in (by.items() if isinstance(by, dict) else []):
        if not isinstance(v, dict) or not isinstance(v.get("played"), (int, float)):
            continue
        played, wins, avg = v["played"], v.get("wins") or 0, v.get("avgScore") or 0
        score = wins / max(1, played) * 0.6 + avg * 0.4 + min(played, 10) * 0.01
        if score > best_score:
            best, best_score = pid, score
    if best is None:
        return BEST_FALLBACK
    v = by[best]
    verb = VERBS.get(best, best).replace("!", "", 1)
    return best, f"лучшие смены — «{verb}»: {v.get('wins') or 0} из {v['played']}, средний счёт {round((v.get('avgScore') or 0) * 100)}"
