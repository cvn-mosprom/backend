"""Landing API: no Telegram identity, no fabricated game user, atomic lead/outbox and safe retries."""
from __future__ import annotations
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .bodies import invalid
from .errors import AppError
from .leads import LeadIn, LeadService, _str, age_on, clean, enqueue, iso, lead_season_id, today
from .models import LandingEvent, Lead
from .schemas import CamelModel


class PublicLeadIn(LeadIn):
    university: _str(200) = ''
    department: Literal['engineering', 'lab', 'bottling', 'other'] = 'other'
    practice_type: Literal['paid', 'job']
    submission_id: UUID
    receipt_token: UUID
    source: _str(60) = 'land_general_form'
    visitor_id: UUID | None = None

    @field_validator('source')
    @classmethod
    def safe_source(cls, v):
        return re.sub(r'[^a-z0-9_]', '_', v.lower())[:60]


def validated_public(body: PublicLeadIn):
    c = clean(body, today())
    if len(c.full_name.split()) < 2:
        raise invalid('fullName', 'First and last name are required')
    if c.birth_date is None or age_on(c.birth_date, today()) < 18:
        raise AppError('age_required', 'This programme requires age 18+')
    if body.consent is not True or not c.consent_version:
        raise AppError('consent_required', 'Consent and its text version are required')
    if body.practice_type == 'paid' and len(c.university.strip()) < 2:
        raise invalid('university', 'University or college is required for practice')
    if c.vacancy:
        expected = {'kipia':'engineering','adjuster':'engineering','lab':'lab','kvasovar':'lab',
                    'operator':'bottling','storekeeper':'bottling','forklift':'bottling'}[c.vacancy]
        if body.department != expected:
            raise invalid('department', 'Department does not match the selected role')
    # Use the same normalised representation for retries, independent of JSON key order / formatting.
    values = dict(full_name=c.full_name, birth_date=c.birth_date, phone=c.phone, email=c.email,
                  telegram=c.telegram, university=c.university.strip() if body.practice_type == 'paid' else '',
                  direction=body.department, vacancy=c.vacancy, readiness=c.readiness, practice_type=c.practice_type,
                  ready_from=c.ready_from, consent=True, consent_text_version=c.consent_version, source=body.source)
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
    return values, digest


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def create_public(svc: LeadService, body: PublicLeadIn) -> dict[str, Any]:
    values, digest = validated_public(body)
    request_id, receipt_hash = str(body.submission_id), token_hash(str(body.receipt_token))
    async with svc.sm() as session, session.begin():
        season_id = await lead_season_id(session, svc.bots_enabled)
        lead = (await session.execute(pg_insert(Lead).values(
            **values, user_id=None, season_id=season_id, submission_id=request_id, submission_hash=digest,
            receipt_token_hash=receipt_hash, visitor_id=str(body.visitor_id) if body.visitor_id else None, consent_at=datetime.now(timezone.utc), tags=[]
        ).on_conflict_do_nothing(constraint='uq_leads_submission_id').returning(Lead))).scalar_one_or_none()
        if lead is None:
            previous = (await session.execute(select(Lead).where(Lead.submission_id == request_id))).scalar_one()
            if (not hmac.compare_digest(previous.receipt_token_hash or '', receipt_hash)
                    or not hmac.compare_digest(previous.submission_hash or '', digest)
                    or previous.consent_revoked_at is not None):
                raise AppError('submission_conflict', 'Submission key is already used', status=409)
            return {'ok': True, 'id': previous.id}
        await enqueue(session, 'lead_created', {
            'lead_id':lead.id,'user_id':None,'tg_id':None,'season_id':season_id,'source':lead.source,
            'full_name':lead.full_name,'birth_date':iso(lead.birth_date),'university':lead.university,
            'direction':lead.direction,'course':None,'vacancy':lead.vacancy,'practice_type':lead.practice_type,
            'readiness':lead.readiness,'phone':lead.phone,'email':lead.email,'telegram':lead.telegram,
            'tags':[],'quiz_score':None,'best_profession':None,'season_place':None,'created_at':iso(lead.created_at)})
        return {'ok':True,'id':lead.id}


async def revoke_public(svc: LeadService, submission_id: UUID, token: str) -> None:
    async with svc.sm() as session, session.begin():
        lead = (await session.execute(select(Lead).where(Lead.submission_id == str(submission_id)).with_for_update())).scalar_one_or_none()
        if not lead or not hmac.compare_digest(lead.receipt_token_hash or '', token_hash(token)):
            raise AppError('lead_not_found', 'Lead not found')
        if lead.consent_revoked_at is not None:
            return
        now = datetime.now(timezone.utc)
        await session.execute(update(Lead).where(Lead.id == lead.id).values(consent_revoked_at=now, updated_at=now))
        await enqueue(session,'lead_revoked',{'lead_id':lead.id,'user_id':None,'tg_id':None,
                                            'season_id':lead.season_id,'revoked_at':iso(now)})


class LandingEventIn(CamelModel):
    submission_id: UUID | None = None
    name: Literal['landing_view','role_view','game_click','form_open','form_view','form_start','lead_attempt','lead_success','lead_error']
    role: Literal['kipia','adjuster','lab','kvasovar','operator','storekeeper','forklift'] | None = None
    entry: Literal['hero','roles','process','game','sticky','form'] | None = None


class LandingBatch(CamelModel):
    session_id: UUID
    visitor_id: UUID | None = None
    source: _str(60) = 'land_general'
    events: list[LandingEventIn] = Field(max_length=20)


async def ingest_landing(svc: LeadService, body: LandingBatch) -> dict[str,int]:
    if body.events:
        source = re.sub(r'[^a-z0-9_]', '_', body.source.lower())[:60]
        rows = [{'session_id':str(body.session_id),'visitor_id':str(body.visitor_id) if body.visitor_id else None,'source':source,'name':e.name,
                 'props':e.model_dump(mode='json',by_alias=True,exclude={'name'},exclude_none=True)} for e in body.events]
        async with svc.sm() as session, session.begin():
            await session.execute(insert(LandingEvent),rows)
    return {'accepted':len(body.events)}
