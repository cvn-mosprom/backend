"""HR CRM, C5: vacancies. Contract: docs/hr-crm-plan.md ("C5 as built"), the web client's `Vacancy` (vendor/frontend lib/hr/types.ts).

  GET   /api/hr/vacancies                   any HR role -> {items: [Vacancy]}  (`matching` = candidates with fit >= 70)
  GET   /api/hr/vacancies/{id}/candidates   any HR role -> {items: [best candidates by fit]}  (?limit=1..50, PII masked for analyst)
  POST  /api/hr/vacancies                   hr_lead+ {professionId, title, city?, active?, requirements?} -> 201 Vacancy
  PATCH /api/hr/vacancies/{id}              hr_lead+ any subset of the same fields -> Vacancy | 404 vacancy_not_found
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import func, select

from ..errors import AppError
from ..hr import vacancies as vac
from ..hr.access import HrActor, hr_actor, mask_pii_many
from ..models import Vacancy
from ..ratelimit import rate_limit
from .webauth import _sm

log = logging.getLogger("mosprom.hr")

router = APIRouter(prefix="/api/hr", tags=["hr"], dependencies=[Depends(rate_limit("hr"))])

AnyHr = Annotated[HrActor, Depends(hr_actor("analyst"))]
LeadHr = Annotated[HrActor, Depends(hr_actor("hr_lead"))]
VacancyId = Annotated[int, Path(gt=0, le=2**62)]


async def _out(session, row: Vacancy) -> dict[str, Any]:
    matching = (await vac.count_matching(session, {row.id: row.requirements}))[row.id]
    return vac.vacancy_out(row, matching)


@router.get("/vacancies")
async def list_vacancies(request: Request, actor: AnyHr) -> dict[str, Any]:
    async with _sm(request)() as session:
        rows = list((await session.execute(select(Vacancy).order_by(Vacancy.id))).scalars())
        counts = await vac.count_matching(session, {r.id: r.requirements for r in rows})
    return {"items": [vac.vacancy_out(r, counts[r.id]) for r in rows]}


@router.post("/vacancies", status_code=201)
async def create_vacancy(body: vac.VacancyCreateIn, request: Request, actor: LeadHr) -> dict[str, Any]:
    async with _sm(request)() as session, session.begin():
        row = Vacancy(profession_id=body.profession_id, title=body.title, city=body.city, active=body.active,
                      requirements=body.requirements.dump(), created_by=actor.id)
        session.add(row)
        await session.flush()
        out = await _out(session, row)
    log.info("vacancy %s created by hr %s", row.id, actor.id)
    return out


@router.patch("/vacancies/{vacancy_id}")
async def patch_vacancy(vacancy_id: VacancyId, body: vac.VacancyPatchIn, request: Request, actor: LeadHr) -> dict[str, Any]:
    async with _sm(request)() as session, session.begin():
        row = (await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id).with_for_update())).scalar_one_or_none()
        if row is None:
            raise AppError("vacancy_not_found", "Vacancy not found")
        sent = body.model_fields_set
        for field in ("profession_id", "title", "city", "active"):
            if field in sent and getattr(body, field) is not None:
                setattr(row, field, getattr(body, field))
        if "requirements" in sent and body.requirements is not None:
            row.requirements = body.requirements.dump()
        row.updated_at = func.now()
        await session.flush()
        out = await _out(session, row)
    log.info("vacancy %s updated by hr %s: %s", vacancy_id, actor.id, sorted(sent))
    return out


@router.get("/vacancies/{vacancy_id}/candidates")
async def vacancy_candidates(vacancy_id: VacancyId, request: Request, actor: AnyHr,
                             limit: Annotated[int, Query(ge=1, le=50)] = 20) -> dict[str, Any]:
    async with _sm(request)() as session:
        row = (await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id))).scalar_one_or_none()
        if row is None:
            raise AppError("vacancy_not_found", "Vacancy not found")
        items = await vac.top_candidates(session, row.requirements, limit)
    return {"items": mask_pii_many(items, actor)}
