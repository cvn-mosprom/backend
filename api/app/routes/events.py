"""Analytics endpoint (S5): POST /api/events {events:[{name, props?, ts}]} -> {accepted}. Logic: app/events.py."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ..auth import CurrentUser
from ..bodies import read_json
from ..events import MAX_BODY_BYTES, EventService
from ..ratelimit import rate_limit

router = APIRouter(prefix="/api", tags=["events"])


def get_events(request: Request) -> EventService:
    return EventService(request.app.state.sessionmaker)


Events = Annotated[EventService, Depends(get_events)]


@router.post("/events", dependencies=[Depends(rate_limit("events"))])
async def events(request: Request, user: CurrentUser, svc: Events) -> dict[str, int]:
    """Batch of up to 50 whitelisted events. 422 too_many_events | invalid_event_name | future_timestamp."""
    return await svc.ingest(user, await read_json(request, MAX_BODY_BYTES))
