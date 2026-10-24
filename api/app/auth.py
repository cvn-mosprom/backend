"""Authentication: Telegram Mini Apps initData (HMAC) + dev mode, `current_user` upsert.

Header: `Authorization: tma <raw initData>` (prod), `Authorization: Bearer <web session token>` (S14, web login through the
bot, app/webauth.py) or `Authorization: dev <tg_id>[:<name>]` (DEV_AUTH=1); the web client
sends `X-Telegram-Init-Data: <raw initData>` or (dev only) `X-Dev-User: <tg_id>` instead, see `credentials()` for the priority.
Stage 1 (`get_identity`) is DB-free and memoised on the request; stage 2 (`current_user`) upserts the user row.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import Depends, Request
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .errors import AppError
from .models import Factory, User
from .settings import Settings
from .testusers import is_test_identity

log = logging.getLogger("mosprom.auth")

AUTH_CACHE_TTL_S = 300
FUTURE_SKEW_S = 300  # tolerated clock skew for an auth_date from the future
LAST_SEEN_THROTTLE = timedelta(seconds=60)
MAX_TG_ID = 2**63 - 1  # users.tg_id is a signed bigint
START_PARAM_MAX = 128


@dataclass(frozen=True)
class Identity:
    tg_id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    start_param: str | None = None
    auth_date: int = 0
    is_test: bool = False  # dev auth only: e2e name or reserved tg_id range (app/testusers.py)


def _invalid(msg: str = "Invalid initData") -> AppError:
    return AppError("invalid_init_data", msg)


def verify_init_data(raw: str, bot_token: str, max_age_s: int, now: float | None = None) -> Identity:
    """Validate raw initData per the Mini Apps spec. Raises AppError invalid_init_data / auth_expired."""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=50)
    except ValueError:
        raise _invalid("Malformed initData") from None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise _invalid("Duplicate initData fields")
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise _invalid("initData has no hash")

    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise _invalid("initData signature mismatch")

    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        tg_id = int(user["id"])
    except (KeyError, ValueError, TypeError, OverflowError):
        raise _invalid("initData is missing auth_date or user") from None
    if not 0 < tg_id <= MAX_TG_ID:
        raise _invalid("initData user is invalid")

    now = time.time() if now is None else now
    if auth_date > now + FUTURE_SKEW_S:
        raise _invalid("initData auth_date is in the future")
    if now - auth_date > max_age_s:
        raise AppError("auth_expired", "initData is too old, reopen the Mini App",
                       details={"max_age_s": max_age_s})

    def s(key: str) -> str | None:
        v = user.get(key)
        return v if isinstance(v, str) and v else None

    return Identity(tg_id=tg_id, first_name=s("first_name"), last_name=s("last_name"), username=s("username"),
                    start_param=fields.get("start_param") or None, auth_date=auth_date)


def parse_dev_header(value: str) -> Identity:
    ident, _, name = value.partition(":")
    try:
        tg_id = int(ident)
    except ValueError:
        raise _invalid("dev auth expects '<tg_id>[:<name>]'") from None
    if not 0 < tg_id <= MAX_TG_ID:
        raise _invalid("dev tg_id must be a positive 64-bit integer")
    return Identity(tg_id=tg_id, first_name=name.strip()[:64] or f"Dev{tg_id}", is_test=is_test_identity(tg_id, name))


def credentials(request: Request) -> tuple[str, str]:
    """(scheme, value) of the caller's credentials. Priority: `Authorization: tma|dev <v>`, then the web client's
    `X-Telegram-Init-Data: <initData>` (= tma), then `X-Dev-User: <tg_id>` (= dev). The first header PRESENT wins even
    when its value is unusable, so a bad `Authorization` is never rescued by a dev header."""
    headers = request.headers
    if (header := headers.get("authorization")) is not None and header.strip():
        scheme, _, value = header.partition(" ")
        return scheme.lower(), value.strip()
    if (init := headers.get("x-telegram-init-data")) is not None:
        if not init.strip():
            raise AppError("unauthorized", "X-Telegram-Init-Data is empty")
        return "tma", init.strip()
    if (dev := headers.get("x-dev-user")) is not None:
        return "dev", dev.strip()
    raise AppError("unauthorized", "Authorization, X-Telegram-Init-Data or X-Dev-User header is required")


async def _bearer_identity(request: Request, token: str) -> Identity:
    """`Authorization: Bearer <token>` -> the Telegram identity of the session's user (so every endpoint sees the same
    tg_id / users row / factory as with initData). Needs the DB; also remembers the session id for DELETE /api/auth/web/session."""
    from . import webauth  # webauth imports this module

    sm = request.app.state.sessionmaker
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    info = await webauth.resolve_bearer(sm, token)
    request.state.web_session_id = info.session_id
    request.state.web_session_expires_at = info.expires_at
    u = info.user
    return Identity(tg_id=u.tg_id, first_name=u.first_name, last_name=u.last_name, username=u.username)


async def _authenticate(request: Request) -> Identity:
    settings: Settings = request.app.state.settings
    scheme, value = credentials(request)

    if scheme == "bearer":  # S14: web session token issued after a login through the Telegram bot (app/webauth.py)
        return await _bearer_identity(request, value)
    if scheme == "dev" and settings.dev_auth_enabled:  # never in prod: APP_ENV=prod forces dev_auth_enabled off
        return parse_dev_header(value)
    if scheme != "tma":  # includes `dev` while DEV_AUTH is off
        raise AppError("unauthorized", "Unsupported authorization scheme")
    if not value:
        raise _invalid("Empty initData")
    if not settings.bot_token:
        raise AppError("config_error", "BOT_TOKEN is not configured")

    redis = request.app.state.redis
    key = "auth:" + hashlib.sha256(value.encode()).hexdigest()
    try:
        cached = await redis.get(key)
    except Exception as e:  # the cache is optional
        log.warning("auth cache read failed: %s", type(e).__name__)
        cached = None
    if cached:
        ident = Identity(**json.loads(cached))
        if time.time() - ident.auth_date <= settings.init_data_max_age_s:  # cache TTL must not extend validity
            return ident

    ident = verify_init_data(value, settings.bot_token, settings.init_data_max_age_s)
    try:
        await redis.set(key, json.dumps(asdict(ident)), ex=AUTH_CACHE_TTL_S)
    except Exception as e:
        log.warning("auth cache write failed: %s", type(e).__name__)
    return ident


async def get_identity(request: Request) -> Identity:
    """Authenticate once per request (result or error is memoised on request.state)."""
    state = request.state
    if (err := getattr(state, "identity_error", None)) is not None:
        raise err
    if (ident := getattr(state, "identity", None)) is not None:
        return ident
    try:
        ident = await _authenticate(request)
    except AppError as e:
        state.identity_error = e
        raise
    state.identity = ident
    state.tg_id = ident.tg_id
    return ident


async def upsert_user(sessionmaker, ident: Identity) -> User:
    """Create on first sight, else refresh last_seen_at / username / names, in its own short transaction.
    Reads first, so the common path writes at most once a minute and never burns sequence values."""
    now = datetime.now(timezone.utc)
    async with sessionmaker() as session, session.begin():
        user = (await session.execute(select(User).where(User.tg_id == ident.tg_id))).scalar_one_or_none()
        if user is not None:
            changed = {
                k: v
                for k, v in (("username", ident.username), ("first_name", ident.first_name),
                             ("last_name", ident.last_name))
                if getattr(user, k) != v
            }
            if user.source is None and ident.start_param:
                changed["source"] = ident.start_param
            if ident.is_test and not user.is_test:  # sticky: a test user stays a test user
                changed["is_test"] = True
            if changed or now - user.last_seen_at > LAST_SEEN_THROTTLE:
                for k, v in changed.items():
                    setattr(user, k, v)
                if changed.get("is_test"):
                    await session.execute(update(Factory).where(Factory.user_id == user.id).values(is_test=True))
                user.last_seen_at = now
            return user
        stmt = (
            pg_insert(User)
            .values(tg_id=ident.tg_id, username=ident.username, first_name=ident.first_name,
                    last_name=ident.last_name, source=ident.start_param, last_seen_at=now,
                    is_test=ident.is_test)
            .on_conflict_do_update(  # lost a creation race: behave like the update path
                index_elements=[User.tg_id], set_={"last_seen_at": now})
            .returning(User)
        )
        return (await session.execute(stmt)).scalar_one()


def clean_start_param(raw: str | None) -> str | None:
    """`?start_param=` of the web client (a Telegram start_param, or `?startapp=` / `?src=` outside Telegram): trimmed;
    empty, over-long or with control characters -> None (ignored, never an error: bootstrap must not fail on it)."""
    value = (raw or "").strip()
    if not value or len(value) > START_PARAM_MAX or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    return value


async def adopt_start_param(sessionmaker, user: User, raw: str | None) -> None:
    """Fallback source for clients outside Telegram: write the query `start_param` into `users.source` only while it is
    empty. initData's own start_param was already stored by `upsert_user`, so it always wins over the query."""
    value = clean_start_param(raw)
    if value is None or user.source:
        return
    async with sessionmaker() as session, session.begin():
        await session.execute(update(User).where(User.id == user.id, or_(User.source.is_(None), User.source == ""))
                              .values(source=value))
    user.source = value


async def current_user(request: Request) -> User:
    ident = await get_identity(request)
    sm = request.app.state.sessionmaker
    if sm is None:
        raise AppError("config_error", "DATABASE_URL is not configured")
    user = await upsert_user(sm, ident)
    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(current_user)]
