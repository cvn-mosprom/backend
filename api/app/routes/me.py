from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends

from ..auth import CurrentUser
from ..ratelimit import rate_limit
from ..schemas import CamelModel

router = APIRouter()


class MeOut(CamelModel):
    id: int
    tg_id: int
    name: str
    username: str | None
    age_confirmed: bool
    created_at: datetime


@router.get("/api/me", response_model=MeOut, dependencies=[Depends(rate_limit("default"))])
async def me(user: CurrentUser) -> MeOut:
    """Auth smoke endpoint: returns the authenticated (upserted) user."""
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or user.username or f"Player {user.tg_id}"
    return MeOut(id=user.id, tg_id=user.tg_id, name=name, username=user.username,
                 age_confirmed=user.age_confirmed, created_at=user.created_at)
