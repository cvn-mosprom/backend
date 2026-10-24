"""HR CRM, C5: analytics and the CSV export. Contract: docs/hr-crm-plan.md ("C5 as built"), the web client's `Analytics`.

  GET /api/hr/analytics?period=7d|30d|all   any HR role (the analyst too) -> funnel, statuses, sources, points, competencyAvg, universities
  GET /api/hr/export.csv?<candidate filters> hr_lead+ -> text/csv (UTF-8 BOM, `;`), Content-Disposition: attachment; writes `export` events
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from ..bodies import NO_CONTROL
from ..hr import analytics as an
from ..hr import candidates as cand
from ..hr import export as exp
from ..hr.access import HrActor, hr_actor
from ..ratelimit import rate_limit
from .hr_candidates import _competencies, _profession, _status
from .webauth import _sm

log = logging.getLogger("mosprom.hr")

router = APIRouter(prefix="/api/hr", tags=["hr"], dependencies=[Depends(rate_limit("hr"))])

AnyHr = Annotated[HrActor, Depends(hr_actor("analyst"))]
LeadHr = Annotated[HrActor, Depends(hr_actor("hr_lead"))]


@router.get("/analytics")
async def analytics(request: Request, actor: AnyHr, period: Literal["7d", "30d", "all"] = Query("30d")) -> dict[str, Any]:
    return await an.cached(request.app, _sm(request), period)


@router.get("/export.csv")
async def export_csv(
    request: Request, actor: LeadHr,
    q: Annotated[str | None, Query(max_length=100, pattern=NO_CONTROL)] = None,
    status: Annotated[list[str], Query(max_length=len(cand.STATUSES) + 1)] = [],  # noqa: B006 (read-only default)
    profession: Annotated[str | None, Query(max_length=40)] = None,
    university: Annotated[str | None, Query(max_length=200, pattern=NO_CONTROL)] = None,
    min_points: Annotated[int | None, Query(alias="minPoints", ge=0, le=1_000_000)] = None,
    min_fit: Annotated[int | None, Query(alias="minFit", ge=0, le=100)] = None,
    assignee: Literal["me", "none"] | None = None,
) -> Response:
    """The filters of `GET /api/hr/candidates` (parsed by C4's helpers); `sort` / `cursor` / `limit` of the list are ignored."""
    f = cand.Filters(q=q, statuses=[_status(s) for s in dict.fromkeys(status)], profession=_profession(profession),
                     university=university or None, min_points=min_points, min_fit=min_fit,
                     competency=_competencies(request), assignee=assignee)
    async with _sm(request)() as session, session.begin():
        rows = await exp.fetch_rows(session, f, actor, bots_enabled=request.app.state.settings.bots_enabled)
        await exp.log_export(session, rows, actor, f)  # the audit trail commits (or fails the export) together with the read
    log.info("hr %s exported %d candidates", actor.id, len(rows))
    return Response(exp.build_csv(rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{exp.filename()}"'})
