"""Lead form endpoints (S5): POST /api/leads, GET /api/leads/me, DELETE /api/leads/me. Logic: app/leads.py.

Request bodies carry personal data: they are never logged (the access log has no bodies or query strings)."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from starlette.responses import Response

from ..auth import CurrentUser
from ..leads import LeadIn, LeadService
from ..ratelimit import rate_limit

router = APIRouter(prefix="/api", tags=["leads"])


def get_leads(request: Request) -> LeadService:
    s = request.app.state
    return LeadService(s.sessionmaker, bots_enabled=s.settings.bots_enabled)


Leads = Annotated[LeadService, Depends(get_leads)]


@router.post("/leads", status_code=201, dependencies=[Depends(rate_limit("leads"))])
async def create_lead(body: LeadIn, user: CurrentUser, leads: Leads) -> dict[str, Any]:
    """Case form -> lead (+ `lead_created` outbox row in the same transaction). 403 age_required | consent_required,
    409 lead_already_exists, 422 validation_error."""
    return await leads.create(user, body)


@router.get("/leads/me", dependencies=[Depends(rate_limit("default"))])
async def my_lead(user: CurrentUser, leads: Leads) -> dict[str, Any]:
    """The player's latest lead (404 lead_not_found)."""
    return await leads.current(user)


@router.delete("/leads/me", status_code=204, response_class=Response, dependencies=[Depends(rate_limit("default"))])
async def revoke_lead(user: CurrentUser, leads: Leads) -> Response:
    """Revoke the consent (152-FZ). Idempotent: 204 again when already revoked, 404 lead_not_found without a lead."""
    await leads.revoke(user)
    return Response(status_code=204)


# Public handlers intentionally have no CurrentUser dependency. Their receipts expose no personal data.
from uuid import UUID
from ..public_landing import PublicLeadIn, LandingBatch, create_public, revoke_public, ingest_landing


@router.post('/leads/public', status_code=201, dependencies=[Depends(rate_limit('public_leads'))])
async def public_lead(body: PublicLeadIn, leads: Leads) -> dict[str, Any]:
    return await create_public(leads, body)


@router.delete('/leads/public/{submission_id}', status_code=204, response_class=Response,
               dependencies=[Depends(rate_limit('public_leads'))])
async def public_revoke(submission_id: UUID, request: Request, leads: Leads) -> Response:
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else ''
    await revoke_public(leads, submission_id, token)
    return Response(status_code=204)


@router.post('/events/public', dependencies=[Depends(rate_limit('landing_events'))])
async def landing_events(body: LandingBatch, leads: Leads) -> dict[str, int]:
    return await ingest_landing(leads, body)
