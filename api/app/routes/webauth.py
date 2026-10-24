"""Web login via the Telegram bot (S14). Contract for the frontend: docs/web-auth.md.

Public (no auth, per-IP rate limited):
  POST /api/auth/web/code   {code}   -> 200 {token, expiresAt, user} | 401 invalid_code | 429
  POST /api/auth/web/start           -> 200 {nonce, botUrl, expiresAt} | 503 config_error (BOT_USERNAME empty)
  POST /api/auth/web/poll   {nonce}  -> 202 {status: "pending"} | 200 {token, expiresAt, user} (once) | 410 login_expired
Bearer-authenticated:
  GET    /api/auth/web/session       -> {user, expiresAt}   (expiresAt null when not a web session)
  DELETE /api/auth/web/session       -> 204 (revokes this web session)
Internal (X-Internal-Token, the bot only; never proxied by caddy):
  POST /internal/auth/login-code {tg_id, username?, first_name?, last_name?} -> {code, expiresAt}
  POST /internal/auth/approve    {nonce, tg_id, username?, first_name?, last_name?} -> {ok: true}   | 410 login_expired
  POST /internal/auth/revoke-all {tg_id}                                      -> {revoked: n}
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from .. import webauth
from ..auth import CurrentUser
from ..errors import AppError
from ..ratelimit import rate_limit
from ..schemas import CamelModel
from .internal import require_internal_token

router = APIRouter(prefix="/api/auth/web", tags=["auth"])
internal_router = APIRouter(prefix="/internal/auth", tags=["internal"], dependencies=[Depends(require_internal_token)])

UserAgent = Annotated[str | None, Header()]


def _sm(request: Request):
    sm = request.app.state.sessionmaker
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    return sm


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _session_body(user, token: str, expires) -> dict[str, Any]:
    return {"token": token, "expiresAt": webauth.iso(expires), "user": webauth.user_out(user)}


# ---------------------------------------------------------------- public

class CodeIn(CamelModel):
    code: str = Field(max_length=32)


class PollIn(CamelModel):
    nonce: str = Field(max_length=webauth.NONCE_MAX_LEN)


@router.post("/code", dependencies=[Depends(rate_limit("auth"))])
async def login_with_code(body: CodeIn, request: Request, user_agent: UserAgent = None) -> dict[str, Any]:
    redis = request.app.state.redis
    ip = _client_ip(request)
    await webauth.check_code_lock(redis, ip)
    code = "".join(body.code.split())
    result = await webauth.redeem_code(_sm(request), code, user_agent) if code.isascii() and code.isdigit() \
        and len(code) == 6 else None
    if result is None:
        await webauth.register_wrong_code(redis, ip)
        raise AppError("invalid_code", "Invalid or expired code")
    await webauth.clear_wrong_codes(redis, ip)
    user, token, expires = result
    return _session_body(user, token, expires)


@router.post("/start", dependencies=[Depends(rate_limit("auth"))])
async def start_login(request: Request) -> dict[str, Any]:
    bot = request.app.state.settings.bot_username
    if not bot:
        raise AppError("config_error", "BOT_USERNAME is not configured")
    nonce, expires = await webauth.create_request(_sm(request))
    return {"nonce": nonce, "botUrl": f"https://t.me/{bot}?start=weblogin_{nonce}", "expiresAt": webauth.iso(expires)}


@router.post("/poll", dependencies=[Depends(rate_limit("auth_poll"))], response_model=None)
async def poll_login(body: PollIn, request: Request, user_agent: UserAgent = None) -> Response:
    result = await webauth.poll_request(_sm(request), body.nonce, user_agent)
    if result is None:
        return JSONResponse({"status": "pending"}, status_code=202)
    user, token, expires = result
    return JSONResponse(_session_body(user, token, expires), headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- bearer session

@router.get("/session", dependencies=[Depends(rate_limit("default"))])
async def get_session(request: Request, user: CurrentUser) -> dict[str, Any]:
    expires = getattr(request.state, "web_session_expires_at", None)
    return {"user": webauth.user_out(user), "expiresAt": webauth.iso(expires) if expires else None}


@router.delete("/session", status_code=204, dependencies=[Depends(rate_limit("default"))])
async def logout(request: Request, user: CurrentUser) -> Response:
    """Revokes the web session this request authenticated with (no-op for tma / dev callers)."""
    session_id = getattr(request.state, "web_session_id", None)
    if session_id is not None:
        await webauth.revoke_session(_sm(request), session_id)
    return Response(status_code=204)


# ---------------------------------------------------------------- internal (the bot)

class BotUserIn(CamelModel):
    tg_id: int
    username: str | None = Field(default=None, max_length=128)
    first_name: str | None = Field(default=None, max_length=128)
    last_name: str | None = Field(default=None, max_length=128)


class ApproveIn(BotUserIn):
    nonce: str = Field(max_length=webauth.NONCE_MAX_LEN)


class RevokeAllIn(CamelModel):
    tg_id: int


async def _bot_user(request: Request, body: BotUserIn):
    return await webauth.upsert_bot_user(_sm(request), request.app.state.settings, body.tg_id, body.username,
                                         body.first_name, body.last_name)


@internal_router.post("/login-code")
async def internal_login_code(body: BotUserIn, request: Request) -> dict[str, Any]:
    user = await _bot_user(request, body)
    code, expires = await webauth.issue_code(_sm(request), user)
    return {"code": code, "expiresAt": webauth.iso(expires)}


@internal_router.post("/approve")
async def internal_approve(body: ApproveIn, request: Request) -> dict[str, Any]:
    user = await _bot_user(request, body)
    await webauth.approve_request(_sm(request), body.nonce, user)
    return {"ok": True}


@internal_router.post("/revoke-all")
async def internal_revoke_all(body: RevokeAllIn, request: Request) -> dict[str, Any]:
    return {"revoked": await webauth.revoke_all(_sm(request), body.tg_id)}
