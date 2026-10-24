"""Web login via the Telegram bot (S14, docs/ARCHITECTURE.md "Web login via Telegram bot", docs/web-auth.md).

The web player proves their Telegram identity through the bot, so web and Telegram are ONE users row (tg_id) and one
factory. Two ways in, both ending with an opaque session token (`Authorization: Bearer <token>`):
  * code:      bot `/login` -> `issue_code` (6 digits, 5 min) -> the player types it on the site -> `redeem_code`
  * deep link: site `create_request` (nonce) -> t.me/<bot>?start=weblogin_<nonce> -> bot `approve_request` -> site `poll_request`
Codes, nonces and tokens are stored only as sha256 and are single use. Everything here is DB work in its own short
transactions (the route layer owns HTTP concerns and rate limits).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from .auth import MAX_TG_ID, Identity, upsert_user
from .errors import AppError
from .models import User, WebLoginCode, WebLoginRequest, WebSession
from .settings import Settings
from .testusers import is_test_identity

log = logging.getLogger("mosprom.webauth")

CODE_TTL = timedelta(minutes=5)
REQUEST_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=30)  # sliding
TOUCH_EVERY = timedelta(hours=1)  # last_seen_at / expires_at are renewed at most this often
PURGE_AFTER = timedelta(days=7)
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32): anything else is rejected without a DB hit
NONCE_MAX_LEN = 128
USER_AGENT_MAX = 200

# Wrong-code lockout. The code endpoint takes the code alone (no tg_id), so a wrong guess cannot be charged to one code;
# it is charged to the caller's IP instead: WRONG_CODE_MAX wrong guesses inside WRONG_CODE_WINDOW_S lock that IP out of
# /api/auth/web/code (429) until the window ends. A success clears the counter.
WRONG_CODE_MAX = 5
WRONG_CODE_WINDOW_S = 600


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"


def display_name(user: User) -> str:
    return " ".join(p for p in (user.first_name, user.last_name) if p) or user.username or f"Player {user.tg_id}"


def user_out(user: User) -> dict:
    return {"id": user.id, "tgId": user.tg_id, "name": display_name(user), "username": user.username}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def clean_ua(value: str | None) -> str | None:
    value = (value or "").strip()[:USER_AGENT_MAX]
    return value or None


# ---------------------------------------------------------------- users created by the bot

async def upsert_bot_user(sm, settings: Settings, tg_id: int, username: str | None, first_name: str | None,
                          last_name: str | None) -> User:
    """Same upsert as `auth` (names / username refreshed). The caller is the bot, i.e. a real Telegram user, so
    `is_test` is only ever set under DEV_AUTH by the dev rules (app/testusers.py: e2e name or reserved id range).
    With no profile fields at all the stored ones are kept (a bare `{tg_id}` call must not wipe the names)."""
    if not 0 < tg_id <= MAX_TG_ID:
        raise AppError("validation_error", "tg_id must be a positive 64-bit integer")
    if username is None and first_name is None and last_name is None:
        async with sm() as session:
            old = (await session.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        if old is not None:
            username, first_name, last_name = old.username, old.first_name, old.last_name
    is_test = settings.dev_auth_enabled and is_test_identity(tg_id, first_name)
    return await upsert_user(sm, Identity(tg_id=tg_id, first_name=first_name, last_name=last_name, username=username,
                                          is_test=is_test))


async def _purge(session, now: datetime) -> None:
    """Housekeeping on the write paths: drop rows that have been dead for a week."""
    cutoff = now - PURGE_AFTER
    await session.execute(delete(WebLoginCode).where(WebLoginCode.expires_at < cutoff))
    await session.execute(delete(WebLoginRequest).where(WebLoginRequest.expires_at < cutoff))
    await session.execute(delete(WebSession).where(or_(WebSession.expires_at < cutoff, WebSession.revoked_at < cutoff)))


# ---------------------------------------------------------------- sessions

async def _new_session(session, user_id: int, user_agent: str | None, now: datetime) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = now + SESSION_TTL
    session.add(WebSession(user_id=user_id, token_hash=sha256_hex(token), created_at=now, last_seen_at=now,
                           expires_at=expires, user_agent=clean_ua(user_agent)))
    await session.flush()
    return token, expires


@dataclass(frozen=True)
class SessionInfo:
    session_id: int
    expires_at: datetime
    user: User


async def resolve_bearer(sm, token: str) -> SessionInfo:
    """`Authorization: Bearer <token>` -> the session and its user. Unknown / revoked -> 401 unauthorized, expired ->
    401 auth_expired. Sliding expiry: at most once per TOUCH_EVERY the session's last_seen_at is bumped and
    expires_at pushed to now + 30 days."""
    token = token.strip()
    if not TOKEN_RE.fullmatch(token):
        raise AppError("unauthorized", "Invalid session token")
    digest = sha256_hex(token)
    now = now_utc()
    async with sm() as session, session.begin():
        row = (await session.execute(
            select(WebSession, User).join(User, User.id == WebSession.user_id).where(WebSession.token_hash == digest)
        )).first()
        if row is None or not hmac.compare_digest(row[0].token_hash, digest):
            raise AppError("unauthorized", "Unknown session token")
        ws, user = row
        if ws.revoked_at is not None:
            raise AppError("unauthorized", "Session was revoked, log in again")
        if ws.expires_at <= now:
            raise AppError("auth_expired", "Session expired, log in again")
        if now - ws.last_seen_at >= TOUCH_EVERY:
            ws.last_seen_at = now
            ws.expires_at = now + SESSION_TTL
        return SessionInfo(session_id=ws.id, expires_at=ws.expires_at, user=user)


async def revoke_session(sm, session_id: int) -> None:
    async with sm() as session, session.begin():
        await session.execute(update(WebSession).where(WebSession.id == session_id, WebSession.revoked_at.is_(None))
                              .values(revoked_at=now_utc()))


async def revoke_all(sm, tg_id: int) -> int:
    """Bot `/logout_web`: every web session of the Telegram user dies, and so do their unused codes and approved but
    not yet collected deep-link logins. Returns the number of revoked sessions (0 for an unknown user)."""
    now = now_utc()
    async with sm() as session, session.begin():
        uid = (await session.execute(select(User.id).where(User.tg_id == tg_id))).scalar_one_or_none()
        if uid is None:
            return 0
        res = await session.execute(update(WebSession).where(WebSession.user_id == uid, WebSession.revoked_at.is_(None))
                                    .values(revoked_at=now))
        await session.execute(update(WebLoginCode).where(WebLoginCode.user_id == uid, WebLoginCode.used_at.is_(None))
                              .values(used_at=now))
        await session.execute(update(WebLoginRequest).where(WebLoginRequest.user_id == uid,
                                                            WebLoginRequest.consumed_at.is_(None))
                              .values(consumed_at=now))
        return res.rowcount or 0


# ---------------------------------------------------------------- flow 1: code

async def issue_code(sm, user: User) -> tuple[str, datetime]:
    """New 6-digit code for the user; their older unused codes die. Two live codes never share the digits (partial
    unique index on the hash), a clash just draws again."""
    now = now_utc()
    expires = now + CODE_TTL
    async with sm() as session, session.begin():
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())  # serialise per user
        await _purge(session, now)
        await session.execute(update(WebLoginCode).where(WebLoginCode.user_id == user.id, WebLoginCode.used_at.is_(None))
                              .values(used_at=now))
        for _ in range(20):
            code = new_code()
            digest = sha256_hex(code)
            await session.execute(update(WebLoginCode).where(  # an expired leftover must not block the digits
                WebLoginCode.code_hash == digest, WebLoginCode.used_at.is_(None), WebLoginCode.expires_at <= now
            ).values(used_at=now))
            try:
                async with session.begin_nested():
                    session.add(WebLoginCode(user_id=user.id, code_hash=digest, created_at=now, expires_at=expires))
                    await session.flush()
                return code, expires
            except IntegrityError:
                continue
    raise AppError("internal_error", "Could not allocate a login code")


async def redeem_code(sm, code: str, user_agent: str | None) -> tuple[User, str, datetime] | None:
    """Exchange a code for a session. One atomic UPDATE ... WHERE used_at IS NULL AND not expired, so a code works once
    even under concurrent requests. None = no such live code (unknown, expired, used or invalidated: indistinguishable)."""
    digest = sha256_hex(code)
    now = now_utc()
    async with sm() as session, session.begin():
        row = (await session.execute(
            update(WebLoginCode).where(WebLoginCode.code_hash == digest, WebLoginCode.used_at.is_(None),
                                       WebLoginCode.expires_at > now)
            .values(used_at=now).returning(WebLoginCode.user_id, WebLoginCode.code_hash)
        )).first()
        if row is None or not hmac.compare_digest(row.code_hash, digest):
            return None
        user = await session.get(User, row.user_id)
        token, expires = await _new_session(session, user.id, user_agent, now)
        return user, token, expires


# ---------------------------------------------------------------- flow 2: deep link

async def create_request(sm) -> tuple[str, datetime]:
    nonce = secrets.token_urlsafe(16)  # 22 chars of [A-Za-z0-9_-]: fits a Telegram start parameter with the prefix
    now = now_utc()
    expires = now + REQUEST_TTL
    async with sm() as session, session.begin():
        await _purge(session, now)
        session.add(WebLoginRequest(nonce_hash=sha256_hex(nonce), created_at=now, expires_at=expires))
    return nonce, expires


def _gone() -> AppError:
    return AppError("login_expired", "This login request has expired or was already used")


async def approve_request(sm, nonce: str, user: User) -> None:
    """Bot: the Telegram user confirmed the login. Idempotent for the same user; expired / unknown / collected / taken
    by someone else -> 410 login_expired."""
    if not nonce or len(nonce) > NONCE_MAX_LEN:
        raise _gone()
    now = now_utc()
    async with sm() as session, session.begin():
        req = (await session.execute(select(WebLoginRequest).where(WebLoginRequest.nonce_hash == sha256_hex(nonce))
                                     .with_for_update())).scalar_one_or_none()
        if req is None or req.expires_at <= now or req.consumed_at is not None:
            raise _gone()
        if req.approved_at is not None:
            if req.user_id == user.id:
                return
            raise _gone()
        req.user_id = user.id
        req.approved_at = now


async def poll_request(sm, nonce: str, user_agent: str | None) -> tuple[User, str, datetime] | None:
    """None = still pending. The first poll after approval returns the session (and marks the request consumed);
    unknown / expired / already collected nonce -> 410 login_expired."""
    if not nonce or len(nonce) > NONCE_MAX_LEN:
        raise _gone()
    now = now_utc()
    async with sm() as session, session.begin():
        req = (await session.execute(select(WebLoginRequest).where(WebLoginRequest.nonce_hash == sha256_hex(nonce))
                                     .with_for_update())).scalar_one_or_none()
        if req is None or req.expires_at <= now or req.consumed_at is not None:
            raise _gone()
        if req.approved_at is None:
            return None
        req.consumed_at = now
        user = await session.get(User, req.user_id)
        token, expires = await _new_session(session, user.id, user_agent, now)
        return user, token, expires


# ---------------------------------------------------------------- wrong-code lockout (per IP, Redis, fails open)

def _fail_key(ip: str) -> str:
    return f"wl:fail:{ip}"


async def check_code_lock(redis, ip: str) -> None:
    try:
        count = await redis.get(_fail_key(ip))
        if count is not None and int(count) >= WRONG_CODE_MAX:
            ttl = await redis.ttl(_fail_key(ip))
            retry = ttl if isinstance(ttl, int) and ttl > 0 else WRONG_CODE_WINDOW_S
            raise AppError("rate_limited", "Too many wrong codes, try again later",
                           details={"retry_after": retry, "limit": WRONG_CODE_MAX, "window_s": WRONG_CODE_WINDOW_S},
                           headers={"Retry-After": str(retry)})
    except AppError:
        raise
    except Exception as e:
        log.warning("code lock check failing open (%s)", type(e).__name__)


async def register_wrong_code(redis, ip: str) -> None:
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.incr(_fail_key(ip))
            pipe.expire(_fail_key(ip), WRONG_CODE_WINDOW_S, nx=True)
            await pipe.execute()
    except Exception as e:
        log.warning("code lock write failing open (%s)", type(e).__name__)


async def clear_wrong_codes(redis, ip: str) -> None:
    try:
        await redis.delete(_fail_key(ip))
    except Exception as e:
        log.warning("code lock clear failing open (%s)", type(e).__name__)
