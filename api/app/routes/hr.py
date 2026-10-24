"""HR CRM, C3: who am I and who may use the CRM. Contract: docs/hr-crm-plan.md ("API", "C3 as built").

  GET   /api/hr/me            any active HR role -> {tgId, name, role, permissions[]}
  GET   /api/hr/users         admin -> {items: [user]}
  POST  /api/hr/users         admin {tgId, role} -> 201 user | 409 hr_user_exists
  PATCH /api/hr/users/{id}    admin {role?, active?} -> user | 409 hr_self_change | 409 hr_last_admin | 404 hr_user_not_found

Auth: the usual one (Bearer web session of S14; tma / dev too). Anonymous 401, no active hr_users row or too low a role 403
`forbidden`. Everything under /api/hr is rate limited as `hr` and answered with `Cache-Control: no-store`
(app.hr.access.NoStoreMiddleware). The candidate routes of C4+ add their own modules and reuse `hr_actor` / `log_event`.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .. import webauth
from ..auth import MAX_TG_ID
from ..errors import AppError
from ..hr.access import HrActor, ensure_admin_remains, hr_actor
from ..models import HrUser, User
from ..ratelimit import rate_limit
from ..schemas import CamelModel
from .webauth import _sm

log = logging.getLogger("mosprom.hr")

router = APIRouter(prefix="/api/hr", tags=["hr"], dependencies=[Depends(rate_limit("hr"))])

AnyHr = Annotated[HrActor, Depends(hr_actor("analyst"))]  # any active HR user, including the read-only analyst
AdminHr = Annotated[HrActor, Depends(hr_actor("admin"))]

Role = Literal["hr", "hr_lead", "analyst", "admin"]


def user_out(row: HrUser, user: User | None) -> dict[str, Any]:
    """`name` / `username` come from the player row (null until the person has played or logged in)."""
    return {
        "id": row.id, "tgId": row.tg_id, "userId": row.user_id if row.user_id is not None else (user.id if user else None),
        "name": webauth.display_name(user) if user is not None else None,
        "username": user.username if user is not None else None,
        "role": row.role, "active": row.active, "createdAt": webauth.iso(row.created_at), "createdBy": row.created_by,
    }


async def _out(session, row_id: int) -> dict[str, Any]:
    row, user = (await session.execute(
        select(HrUser, User).outerjoin(User, User.tg_id == HrUser.tg_id).where(HrUser.id == row_id))).one()
    return user_out(row, user)


@router.get("/me")
async def hr_me(actor: AnyHr) -> dict[str, Any]:
    return {"tgId": actor.tg_id, "name": actor.name, "role": actor.role, "permissions": actor.permissions}


# ---------------------------------------------------------------- users (admin)

class UserCreateIn(CamelModel):
    tg_id: int = Field(gt=0, le=MAX_TG_ID)
    role: Role


class UserPatchIn(CamelModel):
    role: Role | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> "UserPatchIn":
        if self.role is None and self.active is None:
            raise ValueError("role or active is required")
        return self


@router.get("/users")
async def list_users(request: Request, actor: AdminHr) -> dict[str, Any]:
    async with _sm(request)() as session:
        rows = (await session.execute(
            select(HrUser, User).outerjoin(User, User.tg_id == HrUser.tg_id).order_by(HrUser.id))).all()
    return {"items": [user_out(row, user) for row, user in rows]}


@router.post("/users", status_code=201)
async def add_user(body: UserCreateIn, request: Request, actor: AdminHr) -> dict[str, Any]:
    async with _sm(request)() as session, session.begin():
        stmt = (
            pg_insert(HrUser)
            .values(tg_id=body.tg_id, role=body.role, active=True, created_by=actor.id,
                    user_id=select(User.id).where(User.tg_id == body.tg_id).scalar_subquery())
            .on_conflict_do_nothing(index_elements=[HrUser.tg_id])
            .returning(HrUser.id)
        )
        new_id = (await session.execute(stmt)).scalar_one_or_none()
        if new_id is None:
            raise AppError("hr_user_exists", "This Telegram id is already an HR user; PATCH it to change the role or reactivate")
        out = await _out(session, new_id)
    log.info("hr_users: %s added tg %s as %s", actor.tg_id, body.tg_id, body.role)
    return out


@router.patch("/users/{hr_user_id}")
async def patch_user(hr_user_id: Annotated[int, Path(gt=0, le=MAX_TG_ID)], body: UserPatchIn, request: Request, actor: AdminHr) -> dict[str, Any]:
    """Change the role and / or the active flag. Guards: nobody changes their own role or deactivates themselves
    (409 `hr_self_change`), and at least one active admin always remains (409 `hr_last_admin`). The active admins are
    locked (FOR UPDATE, id order) before the check, so two admins demoting each other at once cannot both succeed."""
    async with _sm(request)() as session, session.begin():
        admins = list((await session.execute(
            select(HrUser).where(HrUser.role == "admin", HrUser.active.is_(True)).order_by(HrUser.id).with_for_update())
        ).scalars())
        if actor.id not in {a.id for a in admins}:  # demoted / deactivated by a concurrent request after authentication
            raise AppError("forbidden", "HR access required")
        target = (await session.execute(
            select(HrUser).where(HrUser.id == hr_user_id).with_for_update())).scalar_one_or_none()
        if target is None:
            raise AppError("hr_user_not_found", "HR user not found")
        new_role = body.role if body.role is not None else target.role
        new_active = body.active if body.active is not None else target.active
        if target.id == actor.id and (new_role != target.role or new_active != target.active):
            raise AppError("hr_self_change", "You cannot change your own role or deactivate yourself")
        ensure_admin_remains(admins, target.id, new_role, new_active)
        if (new_role, new_active) != (target.role, target.active):
            log.info("hr_users: %s set %s role %s->%s active %s->%s", actor.tg_id, target.tg_id, target.role, new_role,
                     target.active, new_active)
            target.role, target.active = new_role, new_active
            await session.flush()
        return await _out(session, target.id)
