"""Achievements for the player (C2): GET /api/achievements/me and GET /api/achievements/catalog. Logic: app/achievements/player.py.

  /me       the caller's catalog with progress, earned list, achievementPoints, seasonPoints, competency (never the fit)
  /catalog  {items: [{id, title, tier, points, category, competency, hidden}]} for any logged-in user (the CRM's requirement picker)
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from ..achievements import player
from ..auth import CurrentUser
from ..errors import AppError
from ..ratelimit import rate_limit

router = APIRouter(prefix="/api/achievements", tags=["achievements"], dependencies=[Depends(rate_limit("default"))])


def _sm(request: Request) -> Any:
    sm = request.app.state.sessionmaker
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    return sm


@router.get("/me")
async def my_achievements(request: Request, user: CurrentUser) -> dict[str, Any]:
    return await player.my_achievements(_sm(request), user)


@router.get("/catalog")
async def catalog(request: Request, user: CurrentUser) -> dict[str, Any]:
    return await player.catalog(_sm(request), user)
