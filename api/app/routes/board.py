"""Leaderboard endpoints (S4): GET /api/season/leaderboard, GET /api/factory/{playerId}, GET /api/season/summary.

Bodies are camelCase like the frontend types (Leaderboard, SeasonSummary, GameApi.visit). Logic: app/leaderboard.py.
Included AFTER routes/game.py so that the literal /api/factory/me wins over /api/factory/{player_id}.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from ..auth import CurrentUser
from ..bodies import NO_CONTROL
from ..leaderboard import LeaderboardService
from ..ratelimit import rate_limit

router = APIRouter(prefix="/api", tags=["leaderboard"])


def get_board(request: Request) -> LeaderboardService:
    s = request.app.state
    return LeaderboardService(s.sessionmaker, s.redis, bots_enabled=s.settings.bots_enabled,
                              cache_ttl_s=s.settings.leaderboard_cache_ttl_s)


Board = Annotated[LeaderboardService, Depends(get_board)]


@router.get("/season/leaderboard", dependencies=[Depends(rate_limit("leaderboard"))])
async def leaderboard(
    user: CurrentUser, board: Board,
    tab: str = "general",  # general | quality | speed | uni, anything else -> 422 invalid_tab
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    university: Annotated[str | None, Query(max_length=200, pattern=NO_CONTROL)] = None,
    university_id: Annotated[str | None, Query(max_length=200, pattern=NO_CONTROL)] = None,  # alias of `university` (the university name)
    season_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
) -> dict[str, Any]:
    """Top-`limit` (cached in Redis 15-30 s) + the caller's `me` block (place, value, gapTo51, inviteCutoff)."""
    return await board.leaderboard(user, tab, limit, university or university_id or None, season_id)


@router.get("/season/summary", dependencies=[Depends(rate_limit("default"))])
async def season_summary(user: CurrentUser, board: Board) -> dict[str, Any]:
    """The player's season result (from the stored snapshot and denormalised columns, no sim call)."""
    return await board.summary(user)


@router.get("/factory/{player_id}", dependencies=[Depends(rate_limit("default"))])
async def visit(player_id: str, user: CurrentUser, board: Board) -> dict[str, Any]:
    """Public view of somebody's factory: player (id, name, university) + factory (name, level, stars, layout, updatedAt)."""
    return await board.visit(player_id)
