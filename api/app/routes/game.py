"""Game endpoints (S2): GET /api/bootstrap, POST /api/command, POST /api/advance, GET /api/factory/me.

Bodies are camelCase for response fields that mirror the frontend types (serverTime, advancedMs, startsAt ...);
request fields follow the spec (idempotency_key, cap_hours). Errors use the unified format (app/errors.py).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

import orjson
from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from ..auth import CurrentUser
from ..contract.generated import Command
from ..game import GameService, command_payload
from ..ratelimit import rate_limit

router = APIRouter(prefix="/api", tags=["game"])


def get_game(request: Request) -> GameService:
    s = request.app.state
    return GameService(s.sessionmaker, s.redis, s.sim)


Game = Annotated[GameService, Depends(get_game)]


def json_response(body: dict[str, Any]) -> Response:
    """Snapshots are large: serialise once with orjson instead of FastAPI's jsonable_encoder."""
    return Response(orjson.dumps(body), media_type="application/json")


class CommandIn(BaseModel):
    version: int = Field(strict=True, ge=0)  # the version the client last saw
    idempotency_key: str = Field(strict=True, min_length=8, max_length=64)
    cmd: Command  # discriminated union generated from types.ts; unknown type / bad payload -> 422 validation_error


class AdvanceIn(BaseModel):
    cap_hours: float = Field(default=8.0, ge=0, le=8)
    reason: Literal["resume", "manual", "background"] = "manual"


@router.get("/bootstrap", dependencies=[Depends(rate_limit("bootstrap"))], response_model=None)
async def bootstrap(user: CurrentUser, game: Game) -> Response:
    """Create-or-load the factory, lazy offline catch-up (8 h cap). Never 409."""
    return json_response(await game.bootstrap(user))


@router.post("/command", dependencies=[Depends(rate_limit("command"))], response_model=None)
async def command(body: CommandIn, user: CurrentUser, game: Game) -> Response:
    """Player command. `buildEnd` is a no-op (no lock/write/version bump); everything else takes the row lock,
    checks the version (409 version_conflict) unless the idempotency key was seen before (replay)."""
    cmd = body.cmd.root
    if cmd.type == "buildEnd":
        return json_response(await game.build_end(user))
    # forward the validated, normalised command (tick keeps its `from` alias); never the raw request
    return json_response(await game.command(user, body.version, body.idempotency_key, command_payload(cmd)))


@router.post("/advance", dependencies=[Depends(rate_limit("advance"))], response_model=None)
async def advance(user: CurrentUser, game: Game, body: Annotated[AdvanceIn | None, Body()] = None) -> Response:
    """Lazy catch-up to now under the row lock. No version in the body, no version check, never 409, no version bump."""
    body = body or AdvanceIn()
    return json_response(await game.advance(user, body.cap_hours, body.reason))


@router.get("/factory/me", dependencies=[Depends(rate_limit("factory_me"))], response_model=None)
async def factory_me(user: CurrentUser, game: Game) -> Response:
    """Resync: catch-up under the lock without a version check. Sim down -> last stored snapshot, degraded=true."""
    return json_response(await game.factory_me(user))
