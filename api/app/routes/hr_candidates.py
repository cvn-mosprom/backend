"""HR CRM, C4: candidates and the pipeline. Contract: vendor/frontend lib/hr/types.ts (+ api.ts), docs/hr-crm-plan.md ("C4 as built").

  GET   /api/hr/candidates               any HR role -> {items: [CandidateRow], nextCursor, total}
        ?q &status* &profession &university &minPoints &minFit &competency.<key>=min &assignee=me|none &sort=fresh|points|fit|rank &cursor &limit
  GET   /api/hr/candidates/{id}          any HR role -> CandidateDetail (writes a `view` event); 404 lead_not_found
  PATCH /api/hr/candidates/{id}          hr+ {status?, assigneeId?, tags?, version} -> CandidateRow | 409 version_conflict
  POST  /api/hr/candidates/{id}/notes    hr+ {text} -> 201 LeadNote
  POST  /api/hr/candidates/{id}/messages hr+ {template: interview|offer|reject|custom, text?} -> 202 | 409 bot_not_started
  GET   /api/hr/pipeline                 any HR role ?profession &assignee -> {columns: [{status, count, items <= 50}]}

The analyst reads everything with masked personal data and no note texts; writes need `hr` or above (403 otherwise).
"""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field, StringConstraints, field_validator, model_validator

from ..bodies import NO_CONTROL, invalid
from ..hr import candidates as cand
from ..hr.access import HrActor, hr_actor
from ..ratelimit import rate_limit
from ..schemas import CamelModel
from .webauth import _sm

log = logging.getLogger("mosprom.hr")

router = APIRouter(prefix="/api/hr", tags=["hr"], dependencies=[Depends(rate_limit("hr"))])

AnyHr = Annotated[HrActor, Depends(hr_actor("analyst"))]
Writer = Annotated[HrActor, Depends(hr_actor("hr"))]
LeadId = Annotated[int, Path(gt=0, le=2**62)]
Status = Literal["new", "screening", "interview", "offer", "hired", "rejected", "reserve"]


def _service(request: Request) -> cand.Candidates:
    return cand.Candidates(_sm(request), bots_enabled=request.app.state.settings.bots_enabled)


def _status(raw: str, field: str = "status") -> str:
    s = cand.LEGACY_STATUS.get(raw, raw)
    if s not in cand.STATUSES:
        raise invalid(field, f"Unknown status, expected one of {', '.join(cand.STATUSES)}")
    return s


def _profession(raw: str | None) -> str | None:
    if raw is not None and raw not in cand.PROFESSIONS:
        raise invalid("profession", f"Unknown profession, expected one of {', '.join(cand.PROFESSIONS)}")
    return raw


def _competencies(request: Request) -> dict[str, int]:
    """`competency.<key>=<min 0..100>` (the client sends one pair; several are ANDed)."""
    out: dict[str, int] = {}
    for name, raw in request.query_params.multi_items():
        if not name.startswith("competency."):
            continue
        key = name[len("competency."):]
        if key not in cand.COMPETENCIES:
            raise invalid(name, f"Unknown competency, expected one of {', '.join(cand.COMPETENCIES)}")
        try:
            value = int(raw)
        except ValueError:
            raise invalid(name, "Expected an integer 0..100") from None
        if not 0 <= value <= 100:
            raise invalid(name, "Expected an integer 0..100")
        out[key] = value
    return out


@router.get("/candidates")
async def list_candidates(
    request: Request, actor: AnyHr,
    q: Annotated[str | None, Query(max_length=100, pattern=NO_CONTROL)] = None,
    status: Annotated[list[str], Query(max_length=len(cand.STATUSES) + 1)] = [],  # noqa: B006 (read-only default)
    profession: Annotated[str | None, Query(max_length=40)] = None,
    university: Annotated[str | None, Query(max_length=200, pattern=NO_CONTROL)] = None,
    min_points: Annotated[int | None, Query(alias="minPoints", ge=0, le=1_000_000)] = None,
    min_fit: Annotated[int | None, Query(alias="minFit", ge=0, le=100)] = None,
    assignee: Literal["me", "none"] | None = None,
    sort: Literal["fresh", "points", "fit", "rank"] = "fresh",
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    f = cand.Filters(q=q, statuses=[_status(s) for s in dict.fromkeys(status)], profession=_profession(profession),
                     university=university or None, min_points=min_points, min_fit=min_fit,
                     competency=_competencies(request), assignee=assignee)
    return await _service(request).page(actor, f, sort, cursor, limit)


@router.get("/pipeline")
async def pipeline(request: Request, actor: AnyHr, profession: Annotated[str | None, Query(max_length=40)] = None,
                   assignee: Literal["me", "none"] | None = None) -> dict[str, Any]:
    return await _service(request).pipeline(actor, cand.Filters(profession=_profession(profession), assignee=assignee))


@router.get("/candidates/{lead_id}")
async def candidate(lead_id: LeadId, request: Request, actor: AnyHr) -> dict[str, Any]:
    return await _service(request).detail(actor, lead_id)


# ---------------------------------------------------------------- writes (hr+)

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=cand.MAX_TAG_LEN, pattern=NO_CONTROL)]


class PatchIn(CamelModel):
    status: Status | Literal["contacted"] | None = None
    assignee_id: Annotated[int, Field(gt=0, le=2**62)] | None = None
    tags: Annotated[list[Tag], Field(max_length=cand.MAX_TAGS)] | None = None
    version: Annotated[int, Field(ge=0, le=2**31 - 1)]

    @model_validator(mode="after")
    def _something(self) -> "PatchIn":
        if not ({"status", "assignee_id", "tags"} & self.model_fields_set):
            raise ValueError("status, assigneeId or tags is required")
        if self.status is None and "status" in self.model_fields_set or self.tags is None and "tags" in self.model_fields_set:
            raise ValueError("status and tags cannot be null")
        return self

    def fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if "status" in self.model_fields_set:
            out["status"] = _status(self.status)  # type: ignore[arg-type]
        if "assignee_id" in self.model_fields_set:
            out["assignee_id"] = self.assignee_id  # explicit null = unassign
        if "tags" in self.model_fields_set:
            out["tags"] = list(dict.fromkeys(self.tags or []))
        return out


@router.patch("/candidates/{lead_id}")
async def patch_candidate(lead_id: LeadId, body: PatchIn, request: Request, actor: Writer) -> dict[str, Any]:
    return await _service(request).patch(actor, lead_id, version=body.version, fields=body.fields())


def _no_nul(v: str) -> str:
    if "\x00" in v:
        raise ValueError("NUL characters are not allowed")
    return v


class NoteIn(CamelModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=cand.MAX_NOTE)]

    _nul = field_validator("text")(_no_nul)


@router.post("/candidates/{lead_id}/notes", status_code=201)
async def add_note(lead_id: LeadId, body: NoteIn, request: Request, actor: Writer) -> dict[str, Any]:
    return await _service(request).add_note(actor, lead_id, body.text)


class MessageIn(CamelModel):
    template: Literal["interview", "offer", "reject", "custom"]
    text: Annotated[str, StringConstraints(strip_whitespace=True, max_length=cand.MAX_MESSAGE)] | None = None

    _nul = field_validator("text")(lambda v: v if v is None else _no_nul(v))

    @model_validator(mode="after")
    def _custom_needs_text(self) -> "MessageIn":
        if not self.text:
            if self.template == "custom":
                raise ValueError("text is required for a custom message")
            self.text = None  # empty = the bot's default text of the template
        return self


@router.post("/candidates/{lead_id}/messages", status_code=202)
async def send_message(lead_id: LeadId, body: MessageIn, request: Request, actor: Writer) -> dict[str, Any]:
    """Queues `hr_message` in the outbox (the bot writes to the candidate's Telegram chat) and logs a `message` event.
    409 `bot_not_started`: the candidate never started the bot (or has no Telegram at all, e.g. a landing lead)."""
    return await _service(request).send_message(actor, lead_id, body.template, body.text)
