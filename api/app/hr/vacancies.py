"""C5: vacancies of the HR CRM and the match of candidates against them.

A vacancy = a game profession + requirements `{"competencies": {key: min 0..100}, "achievements": [catalog ids]}`. The fit of a candidate
to a vacancy is `app.achievements.scores.vacancy_fit(competency, earned_ids, requirements)` (C2): this module only gathers the inputs
(the `player_scores.competency` of the candidate's current season and the set of earned achievement ids) and counts. A candidate is "matching" a
vacancy with `fit >= MATCH_MIN` (the contract of the web client: `Vacancy.matching`).

Candidate pool for matching = distinct players with a lead that is not revoked; demo bots and test users never; leads of the
public landing (no player row) have no game profile, so no fit.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any

from pydantic import Field, StringConstraints, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..achievements import catalog
from ..schemas import CamelModel

MATCH_MIN = 70  # fit >= this counts as "matching" (web client: "кандидатов с fit ≥ 70")
MAX_ACHIEVEMENTS = 60
EMPTY_REQUIREMENTS: dict[str, Any] = {"competencies": {}, "achievements": []}

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
City = Annotated[str, StringConstraints(strip_whitespace=True, max_length=80)]
Score = Annotated[int, Field(strict=True, ge=0, le=100)]


class RequirementsIn(CamelModel):
    competencies: dict[str, Score] = Field(default_factory=dict)
    achievements: list[str] = Field(default_factory=list, max_length=MAX_ACHIEVEMENTS)

    @field_validator("competencies")
    @classmethod
    def _known_competencies(cls, v: dict[str, int]) -> dict[str, int]:
        bad = sorted(set(v) - set(catalog.COMPETENCIES))
        if bad:
            raise ValueError(f"unknown competency {bad[0]!r}; known: {', '.join(catalog.COMPETENCIES)}")
        return v

    @field_validator("achievements")
    @classmethod
    def _known_achievements(cls, v: list[str]) -> list[str]:
        bad = [a for a in v if a not in catalog.BY_ID]
        if bad:
            raise ValueError(f"unknown achievement {bad[0]!r}")
        return list(dict.fromkeys(v))  # a duplicate is the same requirement

    def dump(self) -> dict[str, Any]:
        return {"competencies": dict(self.competencies), "achievements": list(self.achievements)}


def _known_profession(v: str) -> str:
    if v not in catalog.PROFESSIONS:
        raise ValueError(f"unknown profession {v!r}; known: {', '.join(catalog.PROFESSIONS)}")
    return v


class VacancyCreateIn(CamelModel):
    profession_id: str
    title: Title
    city: City = ""
    active: bool = True
    requirements: RequirementsIn = Field(default_factory=RequirementsIn)

    @field_validator("profession_id")
    @classmethod
    def _prof(cls, v: str) -> str:
        return _known_profession(v)


class VacancyPatchIn(CamelModel):
    """Every field optional, at least one required. `requirements`, when sent, replaces the whole object."""

    profession_id: str | None = None
    title: Title | None = None
    city: City | None = None
    active: bool | None = None
    requirements: RequirementsIn | None = None

    @field_validator("profession_id")
    @classmethod
    def _prof(cls, v: str | None) -> str | None:
        return None if v is None else _known_profession(v)

    @model_validator(mode="after")
    def _something(self) -> "VacancyPatchIn":
        if not self.model_fields_set or all(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("at least one field is required")
        return self


def vacancy_out(row: Any, matching: int = 0) -> dict[str, Any]:
    """Wire shape of the web client's `Vacancy`."""
    req = row.requirements or {}
    return {
        "id": row.id, "professionId": row.profession_id, "title": row.title, "city": row.city, "active": row.active,
        "requirements": {"competencies": dict(req.get("competencies") or {}), "achievements": list(req.get("achievements") or [])},
        "matching": matching,
    }


# ---------------------------------------------------------------- fit

def fit_result(competency: Mapping[str, Any] | None, earned_ids: Iterable[str], requirements: Mapping[str, Any]) -> dict[str, Any]:
    """`vacancy_fit` of C2 normalised to `{"score": int 0..100, "reasons": [{"text", "delta"}...]}` whatever shape it returns
    (a bare number, a mapping, an object with `.score` / `.reasons`)."""
    from ..achievements import scores  # imported late: the module is C2's

    res = scores.vacancy_fit(dict(competency or {}), set(earned_ids), requirements)
    if isinstance(res, (int, float)):
        return {"score": int(round(res)), "reasons": []}
    get = res.get if isinstance(res, Mapping) else (lambda k, d=None: getattr(res, k, d))
    return {"score": int(round(get("score", 0) or 0)), "reasons": list(get("reasons", None) or [])}


# the pool of candidates with their game profile: the score row of the player's current season (as in the C4 list) + every earned achievement id
CANDIDATES_SQL = """
SELECT c.user_id, c.lead_id, c.full_name, ps.competency, ps.achievement_points, coalesce(ea.ids, '{}') AS earned
FROM (
  SELECT DISTINCT ON (l.user_id) l.user_id, l.id AS lead_id, l.full_name
  FROM leads l JOIN users u ON u.id = l.user_id
  WHERE u.is_bot = false AND u.is_test = false AND l.consent_revoked_at IS NULL {only}
  ORDER BY l.user_id, l.created_at DESC, l.id DESC
) c
LEFT JOIN LATERAL (SELECT s.competency, s.achievement_points FROM factories f
                   JOIN player_scores s ON s.user_id = f.user_id AND s.season_id = f.season_id
                   WHERE f.user_id = c.user_id) ps ON true
LEFT JOIN LATERAL (SELECT array_agg(DISTINCT achievement_id) AS ids FROM achievements_earned WHERE user_id = c.user_id) ea ON true
"""


async def load_candidates(session: AsyncSession, user_ids: Sequence[int] | None = None) -> list[dict[str, Any]]:
    sql = CANDIDATES_SQL.replace("{only}", "AND l.user_id = ANY(CAST(:uids AS bigint[]))" if user_ids is not None else "")
    rows = (await session.execute(text(sql), {"uids": list(user_ids)} if user_ids is not None else {})).mappings().all()
    return [dict(r) for r in rows]


def fit_for(candidate: Mapping[str, Any], requirements: Mapping[str, Any]) -> dict[str, Any]:
    return fit_result(candidate.get("competency"), candidate.get("earned") or (), requirements)


async def count_matching(session: AsyncSession, requirements_by_id: Mapping[int, Mapping[str, Any]]) -> dict[int, int]:
    """vacancy id -> number of candidates with fit >= MATCH_MIN."""
    out = {vid: 0 for vid in requirements_by_id}
    if not requirements_by_id:
        return out
    for cand in await load_candidates(session):
        if not cand["competency"] and not cand["earned"]:
            continue  # no game profile yet: nothing to match
        for vid, req in requirements_by_id.items():
            if fit_for(cand, req)["score"] >= MATCH_MIN:
                out[vid] += 1
    return out


async def top_candidates(session: AsyncSession, requirements: Mapping[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """Best candidates for one vacancy: `[{id (lead), fullName, achievementPoints, score, reasons}]`, best fit first."""
    ranked: list[dict[str, Any]] = []
    for cand in await load_candidates(session):
        if not cand["competency"] and not cand["earned"]:
            continue
        fit = fit_for(cand, requirements)
        ranked.append({"id": cand["lead_id"], "fullName": cand["full_name"],
                       "achievementPoints": int(cand["achievement_points"] or 0), **fit})
    ranked.sort(key=lambda r: (-r["score"], -r["achievementPoints"], r["id"]))
    return ranked[:limit]


async def fits_for_user(session: AsyncSession, user_id: int, *, only_active: bool = True) -> list[dict[str, Any]]:
    """Helper for the candidate card (C4): the fit of one player to every vacancy ->
    `[{vacancyId, professionId, title, active, score, reasons}]`, best first. Empty when the user has no candidate row."""
    cands = await load_candidates(session, [user_id])
    if not cands:
        return []
    rows = (await session.execute(text(
        "SELECT id, profession_id, title, active, requirements FROM vacancies "
        "WHERE (:only_active = false OR active) ORDER BY id"), {"only_active": only_active})).mappings().all()
    out = [{"vacancyId": r["id"], "professionId": r["profession_id"], "title": r["title"], "active": r["active"],
            **fit_for(cands[0], r["requirements"] or EMPTY_REQUIREMENTS)} for r in rows]
    out.sort(key=lambda r: (-r["score"], r["vacancyId"]))
    return out
