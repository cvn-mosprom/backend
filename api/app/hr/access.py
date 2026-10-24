"""C3: roles and access of the HR CRM (`/api/hr/*`), the candidate event log, PII masking, the env bootstrap.

Roles (table `hr_users`, one row per Telegram id; `HR_ADMIN_TG_IDS` only seeds `admin` rows when absent):

    analyst < hr < hr_lead < admin      (`hr_actor(min_role)` is a floor in this order)

`analyst` is the read-only floor: candidates list + analytics + vacancies, every PII field masked, no writes. The
other three can see PII and write; `hr_lead` adds vacancies write + export, `admin` adds user management.

Auth is the existing one (`app.auth`): `Authorization: Bearer <web session>` (S14) is the main path; tma / dev work as
well, so e2e and dev tooling need no extra scheme. Anonymous -> 401 (from auth), authenticated without an active
`hr_users` row or with a role below `min_role` -> 403 `forbidden`.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from ..auth import current_user
from ..errors import AppError
from ..models import HR_ROLES, HrUser, LeadEvent, User
from ..webauth import display_name

log = logging.getLogger("mosprom.hr")

ROLES = HR_ROLES
ROLE_RANK: dict[str, int] = {"analyst": 0, "hr": 1, "hr_lead": 2, "admin": 3}

# role -> permissions. `GET /api/hr/me` returns the actor's list; the web client hides what it cannot do.
_ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "analyst": ("candidates.read", "analytics.read", "vacancies.read"),
    "hr": ("candidates.read", "candidates.pii", "candidates.write", "analytics.read", "vacancies.read"),
    "hr_lead": ("candidates.read", "candidates.pii", "candidates.write", "analytics.read", "vacancies.read",
                "vacancies.write", "export"),
    "admin": ("candidates.read", "candidates.pii", "candidates.write", "analytics.read", "vacancies.read",
              "vacancies.write", "export", "users.manage"),
}
PERMISSIONS = frozenset(p for perms in _ROLE_PERMISSIONS.values() for p in perms)

EVENT_KINDS = ("view", "status", "assign", "note", "message", "export", "tag", "achievement")  # achievement: C2, system-written


def permissions_of(role: str) -> list[str]:
    return list(_ROLE_PERMISSIONS.get(role, ()))


@dataclass(frozen=True)
class HrActor:
    """The authenticated HR user of this request (`request.state.hr_actor`)."""

    id: int  # hr_users.id: what lead_events.actor_hr_id / created_by refer to
    tg_id: int
    user_id: int | None
    name: str
    role: str

    @property
    def permissions(self) -> list[str]:
        return permissions_of(self.role)

    def can(self, permission: str) -> bool:
        return permission in _ROLE_PERMISSIONS.get(self.role, ())

    @property
    def pii(self) -> bool:
        return self.can("candidates.pii")


def at_least(role: str, min_role: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[min_role]


# ---------------------------------------------------------------- dependency

def hr_actor(min_role: str = "hr") -> Callable[[Request], Awaitable[HrActor]]:
    """FastAPI dependency factory: `Depends(hr_actor("hr_lead"))`. `min_role="analyst"` = any active HR user (read access),
    `"hr"` = may write (analyst excluded). Unknown names fail at import time. Declare it after `rate_limit("hr")`."""
    if min_role not in ROLE_RANK:
        raise KeyError(f"unknown role {min_role!r}; known: {sorted(ROLE_RANK, key=ROLE_RANK.get)}")

    async def dependency(request: Request) -> HrActor:
        user = await current_user(request)  # 401 when anonymous / bad token (nothing HR-specific leaks)
        await ensure_seeded(request.app)
        sm = request.app.state.sessionmaker
        async with sm() as session:
            row = (await session.execute(select(HrUser).where(HrUser.tg_id == user.tg_id))).scalar_one_or_none()
        if row is None or not row.active:
            raise AppError("forbidden", "HR access required")
        if not at_least(row.role, min_role):
            raise AppError("forbidden", f"Role {min_role} or higher required", details={"required": min_role})
        if row.user_id is None:  # first time we see the person as a player: link the rows (best effort)
            async with sm() as session, session.begin():
                await session.execute(update(HrUser).where(HrUser.id == row.id, HrUser.user_id.is_(None))
                                      .values(user_id=user.id))
        actor = HrActor(id=row.id, tg_id=row.tg_id, user_id=user.id, name=display_name(user), role=row.role)
        request.state.hr_actor = actor
        return actor

    dependency.__name__ = f"hr_actor_{min_role}"
    return dependency


# ---------------------------------------------------------------- env bootstrap

async def seed_admins(sm, tg_ids: Iterable[int]) -> list[int]:
    """Idempotent: every id without an `hr_users` row gets an active `admin` row; existing rows (any role, active or not)
    are never touched, so an admin's later changes survive restarts. Returns the ids inserted by this call."""
    inserted: list[int] = []
    async with sm() as session, session.begin():
        for tg in tg_ids:
            stmt = (
                pg_insert(HrUser)
                .values(tg_id=tg, role="admin", active=True,
                        user_id=select(User.id).where(User.tg_id == tg).scalar_subquery())
                .on_conflict_do_nothing(index_elements=[HrUser.tg_id])
                .returning(HrUser.id)
            )
            if (await session.execute(stmt)).first() is not None:
                inserted.append(tg)
    return inserted


async def ensure_seeded(app: FastAPI) -> None:
    """Seed once per process (lifespan does it at startup; this retries lazily when the database was not up then).
    Never raises: a seeding failure must not turn into a 5xx for the request."""
    state = app.state
    if getattr(state, "hr_seeded", False):
        return
    ids = state.settings.hr_admin_ids
    if not ids or state.sessionmaker is None:
        state.hr_seeded = True
        return
    try:
        inserted = await asyncio.wait_for(seed_admins(state.sessionmaker, ids), timeout=10)
    except Exception as e:
        log.warning("HR_ADMIN_TG_IDS seeding failed (%s); retried on the next HR request", type(e).__name__)
        return
    state.hr_seeded = True
    if inserted:
        log.info("seeded %d admin(s) into hr_users from HR_ADMIN_TG_IDS", len(inserted))


# ---------------------------------------------------------------- user-management guards

def ensure_admin_remains(active_admins: Iterable[Any], target_id: int, new_role: str, new_active: bool) -> None:
    """409 `hr_last_admin` when applying (new_role, new_active) to `target_id` would leave no active admin. `active_admins` are
    the rows (with `.id`) that are admins and active BEFORE the change. Through the API an active admin acting on someone else
    always leaves themselves, so this is the belt behind the self-change guard and the row locks (concurrent demotions)."""
    remaining = sum(1 for a in active_admins if a.id != target_id) + (1 if new_role == "admin" and new_active else 0)
    if remaining == 0:
        raise AppError("hr_last_admin", "At least one active admin must remain")


# ---------------------------------------------------------------- PII masking (analyst)

PII_FIELDS = frozenset({
    "fullName", "full_name", "phone", "email", "telegram", "birthDate", "birth_date",
    "hrNote", "hr_note", "tgUsername", "tg_username",  # free text / handles: may carry personal data too
})


def mask_pii(item: Mapping[str, Any], actor: HrActor | None, *, extra: Iterable[str] = ()) -> dict[str, Any]:
    """Copy of a candidate dict that is safe for `actor`: an actor with `candidates.pii` gets it unchanged, anybody else
    (analyst, `None`) gets every PII field set to null and `fullName` replaced by "Кандидат #<id>". Always adds
    `piiMasked` so the client can tell. Works for camelCase and snake_case keys; `extra` masks more keys."""
    out = dict(item)
    masked = actor is None or not actor.pii
    out["piiMasked"] = masked
    if not masked:
        return out
    for key in PII_FIELDS.union(extra):
        if key in out:
            out[key] = None
    placeholder = f"Кандидат #{out['id']}" if out.get("id") is not None else None
    for key in ("fullName", "full_name"):
        if key in item:
            out[key] = placeholder
    return out


def mask_pii_many(items: Iterable[Mapping[str, Any]], actor: HrActor | None, *,
                  extra: Iterable[str] = ()) -> list[dict[str, Any]]:
    extra = tuple(extra)
    return [mask_pii(i, actor, extra=extra) for i in items]


# ---------------------------------------------------------------- candidate event log

async def log_event(session, lead_id: int, actor: HrActor | None, kind: str,
                    payload: Mapping[str, Any] | None = None) -> LeadEvent:
    """Append a `lead_events` row inside the caller's transaction (it commits / rolls back with the change it describes).
    `actor=None` = the system. `payload` must be JSON-serialisable (ids, status names, counts - never PII)."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown lead event kind {kind!r}; known: {EVENT_KINDS}")
    event = LeadEvent(lead_id=lead_id, actor_hr_id=actor.id if actor is not None else None, kind=kind,
                      payload=dict(payload or {}))
    session.add(event)
    await session.flush()
    return event


async def record_event(sm, lead_id: int, actor: HrActor | None, kind: str,
                       payload: Mapping[str, Any] | None = None) -> LeadEvent:
    """`log_event` in its own short transaction (read-only routes such as the card view)."""
    async with sm() as session, session.begin():
        return await log_event(session, lead_id, actor, kind, payload)


# ---------------------------------------------------------------- Cache-Control: no-store on /api/hr/*

class NoStoreMiddleware:
    """Every response under `/api/hr/` (also errors and 429s) carries `Cache-Control: no-store`: candidate data must not
    sit in a browser or proxy cache. Pure ASGI, added outside RequestContextMiddleware so timeouts / 500s are covered."""

    def __init__(self, app: ASGIApp, prefix: str = "/api/hr") -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == self.prefix or path.startswith(self.prefix + "/")):
            await self.app(scope, receive, send)
            return

        async def wrapped(message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, wrapped)
