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

from ..auth import CurrentUser, adopt_start_param
from ..bodies import NO_CONTROL, reject_nul
from ..contract.generated import Command
from ..game import GameService, command_payload
from ..ratelimit import rate_limit

router = APIRouter(prefix="/api", tags=["game"])


def get_game(request: Request) -> GameService:
    s = request.app.state
    return GameService(s.sessionmaker, s.redis, s.sim, bots_enabled=s.settings.bots_enabled)


Game = Annotated[GameService, Depends(get_game)]


def json_response(body: dict[str, Any]) -> Response:
    """Snapshots are large: serialise once with orjson instead of FastAPI's jsonable_encoder."""
    return Response(orjson.dumps(body), media_type="application/json")


def lift_short(body: dict[str, Any]) -> dict[str, Any]:
    """The web client reads `toast` / `rewards` at the top level of the command response (http-contract.md); they stay in
    `result` too (the stored short result, e2e and the old client)."""
    result = body.get("result")
    if isinstance(result, dict):
        for key in ("toast", "rewards"):
            if result.get(key) is not None:
                body[key] = result[key]
    return body


class CommandIn(BaseModel):
    version: int = Field(strict=True, ge=0)  # the version the client last saw
    idempotency_key: str = Field(strict=True, min_length=8, max_length=64, pattern=NO_CONTROL)
    cmd: Command  # discriminated union generated from types.ts; unknown type / bad payload -> 422 validation_error


class AdvanceIn(BaseModel):
    cap_hours: float = Field(default=8.0, ge=0, le=8)
    # `heartbeat` (every ~30 s while the tab is visible) and `order_due` (a server moment of an order / boost) are the web
    # client's reasons; the sim only knows resume | manual | background, so those two reach it as `manual`.
    reason: Literal["resume", "manual", "background", "heartbeat", "order_due"] = "manual"

    @property
    def sim_reason(self) -> str:
        return "manual" if self.reason in ("heartbeat", "order_due") else self.reason


@router.get("/bootstrap", dependencies=[Depends(rate_limit("bootstrap"))], response_model=None)
async def bootstrap(request: Request, user: CurrentUser, game: Game, start_param: str | None = None) -> Response:
    """Create-or-load the factory, lazy offline catch-up (8 h cap). Never 409. `?start_param=` is the traffic source
    outside Telegram (initData's own start_param wins); it fills `users.source` only while that is empty."""
    await adopt_start_param(request.app.state.sessionmaker, user, start_param)
    return json_response(await game.bootstrap(user))


@router.post("/command", dependencies=[Depends(rate_limit("command"))], response_model=None)
async def command(body: CommandIn, user: CurrentUser, game: Game) -> Response:
    """Player command. `buildEnd` is a no-op (no lock/write/version bump); everything else takes the row lock,
    checks the version (409 version_conflict) unless the idempotency key was seen before (replay)."""
    cmd = body.cmd.root
    if cmd.type == "buildEnd":
        return json_response(await game.build_end(user))
    # forward the validated, normalised command (tick keeps its `from` alias); never the raw request
    payload = command_payload(cmd)
    reject_nul(payload, "cmd")  # state.meta / jsonb cannot hold \u0000
    return json_response(lift_short(await game.command(user, body.version, body.idempotency_key, payload)))


@router.post("/advance", dependencies=[Depends(rate_limit("advance"))], response_model=None)
async def advance(user: CurrentUser, game: Game, body: Annotated[AdvanceIn | None, Body()] = None) -> Response:
    """Lazy catch-up to now under the row lock. No version in the body, no version check, never 409, no version bump."""
    body = body or AdvanceIn()
    return json_response(await game.advance(user, body.cap_hours, body.sim_reason))


@router.get("/factory/me", dependencies=[Depends(rate_limit("factory_me"))], response_model=None)
async def factory_me(user: CurrentUser, game: Game) -> Response:
    """Resync: catch-up under the lock without a version check. Sim down -> last stored snapshot, degraded=true."""
    return json_response(await game.factory_me(user))
