"""Internal API for the bot's scheduled jobs (docs/ARCHITECTURE.md, "Round 3 coordination").

Protected by the shared secret `INTERNAL_TOKEN` in header `X-Internal-Token` (constant-time compare; missing / wrong /
unconfigured -> 401 unauthorized). Not rate limited, not exposed by caddy (it proxies only /api/*, /health, /ready) and
exempt from the request timeout (middleware.NO_TIMEOUT_PREFIXES): these calls do long background work.
The bot never touches factory state itself, it only calls these endpoints.

  POST /internal/seasons/rotate   close due seasons (freeze results), open the next one      -> {closed: [ids], opened: id|null}
  POST /internal/top/advance      {limit: 1..1000 (200)} lazy catch-up of the top players    -> {advanced, skipped, failed, ms, ...}
  POST /internal/bots/grow        grow the demo bots to the current season progress          -> {bots, season_id, progress}
"""
from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from .. import seasons
from ..errors import AppError
from ..game import GameService
from ..seed_bots import grow_current
from ..sim_client import now_ms

log = logging.getLogger("mosprom.internal")


async def require_internal_token(request: Request, x_internal_token: Annotated[str | None, Header()] = None) -> None:
    expected = request.app.state.settings.internal_token.strip()
    if not expected or not x_internal_token or not hmac.compare_digest(
            x_internal_token.encode("utf-8"), expected.encode("utf-8")):
        raise AppError("unauthorized", "Invalid or missing internal token")


router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(require_internal_token)])


class TopAdvanceIn(BaseModel):
    limit: int = Field(default=200, ge=1, le=1000)


def _game(request: Request) -> GameService:
    s = request.app.state
    return GameService(s.sessionmaker, s.redis, s.sim, bots_enabled=s.settings.bots_enabled)


@router.post("/seasons/rotate")
async def rotate_seasons(request: Request) -> dict[str, Any]:
    """Idempotent: with nothing due it returns {closed: [], opened: null}. A newly opened season gets the demo bots
    right away (best effort: the 10 min growth job would do it anyway)."""
    sm = request.app.state.sessionmaker
    bots_enabled = request.app.state.settings.bots_enabled
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    async with sm() as session, session.begin():
        rotation = await seasons.rotate(session, now_ms(), bots_enabled=bots_enabled, heavy=True)
    if rotation.opened is not None:
        try:
            async with sm() as session, session.begin():
                await grow_current(session, now_ms())
        except Exception:
            log.exception("bot growth after season rotation failed")
    return rotation.as_dict()


@router.post("/top/advance")
async def top_advance(request: Request, body: TopAdvanceIn | None = None) -> dict[str, Any]:
    """Lazy catch-up of the top-`limit` players through the sim (reason=background, no version bump, locked rows are
    skipped, sim down stops the run). Always 200 with the counters; `sim_down` tells that the run was cut short."""
    return await _game(request).top_advance((body or TopAdvanceIn()).limit)


@router.post("/bots/grow")
async def grow_bots_route(request: Request) -> dict[str, Any]:
    sm = request.app.state.sessionmaker
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    async with sm() as session, session.begin():
        return await grow_current(session, now_ms())
