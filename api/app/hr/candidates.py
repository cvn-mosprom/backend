"""C4: candidates of the HR CRM - list, card, pipeline, PATCH with a version check, notes, messages through the bot.

Contract: vendor/frontend/web/src/lib/hr/types.ts (+ api.ts for the query string); docs/hr-crm-plan.md ("C4 as built").

A candidate is a non-revoked `leads` row whose owner (if any) is neither a demo bot nor a test player. Landing leads
(`user_id IS NULL`, no game account) are candidates too: their game profile is empty, they cannot be messaged.

The list is ONE query: `leads` joined with the game side (`users`, `factories`, `player_scores`, the live season place),
the assignee and the best fit (lateral over `player_scores.fit`). Filters and keyset pagination sit on top of that
subquery, so sort keys, filters and the cursor all use the same computed columns. The roles live in the routes
(`analyst` reads with masked PII, `hr`+ writes); everything here receives an `HrActor` and masks accordingly.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..achievements import catalog
from ..bodies import invalid
from ..errors import AppError
from ..leads import quiz_counts
from ..models import (AchievementEarned, AchievementProgress, Factory, HrUser, Lead, LeadEvent, LeadNote, Outbox, User)
from . import vacancies
from .access import HrActor, log_event, mask_pii

log = logging.getLogger("mosprom.hr")

STATUSES = ("new", "screening", "interview", "offer", "hired", "rejected", "reserve")
LEGACY_STATUS = {"contacted": "screening"}  # the pre-C4 name, still accepted from old links / clients
SORTS = ("fresh", "points", "fit", "rank")
TEMPLATES = ("interview", "offer", "reject", "custom")
COMPETENCIES: tuple[str, ...] = catalog.COMPETENCIES
PROFESSIONS: tuple[str, ...] = tuple(catalog.PROFESSIONS)
PROFESSION_TITLES = {  # vendor/frontend web/src/lib/content/professions.ts (the same titles the bot uses)
    "kvasovar": "Мастер-технолог (квасовар)", "operator": "Оператор линии розлива", "adjuster": "Наладчик оборудования",
    "lab": "Лаборант ОТК", "storekeeper": "Кладовщик", "forklift": "Водитель погрузчика", "kipia": "Инженер КИПиА",
}
DEFAULT_ROLE = "operator"  # `roleId` is required by the client: leads without a vacancy and without a best fit get this one
PIPELINE_ITEMS = 50
HISTORY_LIMIT = 200
NOTES_LIMIT = 200
MAX_TAGS, MAX_TAG_LEN = 20, 40
MAX_NOTE, MAX_MESSAGE = 4000, 3500
MAX_PLACE = 2_147_483_647

# ---------------------------------------------------------------- the candidate subquery

_RANK_ALL = """(
    SELECT o.user_id, row_number() OVER (PARTITION BY o.season_id ORDER BY o.stars DESC, o.user_id ASC) AS place
    FROM factories o WHERE NOT o.is_test AND (:bots OR NOT o.is_bot)
) rk ON rk.user_id = l.user_id"""
# one candidate: count the players ahead instead of ranking everybody (same order as leaderboard.rank_of)
_RANK_ONE = """LATERAL (
    SELECT 1 + count(*) AS place FROM factories o
    WHERE o.season_id = f.season_id AND o.user_id <> f.user_id AND NOT o.is_test AND (:bots OR NOT o.is_bot)
      AND (o.stars > f.stars OR (o.stars = f.stars AND o.user_id < f.user_id))
) rk ON f.user_id IS NOT NULL"""

_BASE_SQL = """
SELECT l.id, l.user_id, l.status, l.created_at, l.stage_changed_at, l.version, l.tags, l.university, l.course, l.vacancy,
       l.best_profession, l.full_name, l.phone, l.email, l.telegram,
       COALESCE(l.source, u.source) AS source,
       l.assignee_id, au.tg_id AS a_tg, au.first_name AS a_first, au.last_name AS a_last, au.username AS a_username,
       COALESCE(ps.achievement_points, 0) AS points,
       CASE WHEN jsonb_typeof(ps.competency) = 'object' THEN ps.competency ELSE '{}'::jsonb END AS competency,
       fb.prof AS fit_prof, COALESCE(fb.score, 0) AS fit_score,
       COALESCE(rk.place, l.season_place) AS place
FROM leads l
LEFT JOIN users u ON u.id = l.user_id
LEFT JOIN factories f ON f.user_id = l.user_id
LEFT JOIN player_scores ps ON ps.user_id = l.user_id AND ps.season_id = f.season_id
LEFT JOIN @RANK@
LEFT JOIN hr_users a ON a.id = l.assignee_id
LEFT JOIN users au ON au.tg_id = a.tg_id
LEFT JOIN LATERAL (
    SELECT e.key AS prof, round((e.value ->> 'score')::numeric)::int AS score
    FROM jsonb_each(CASE WHEN jsonb_typeof(ps.fit) = 'object' THEN ps.fit ELSE '{}'::jsonb END) e
    WHERE jsonb_typeof(e.value -> 'score') = 'number'
    ORDER BY 2 DESC, 1 ASC LIMIT 1
) fb ON true
WHERE l.consent_revoked_at IS NULL AND NOT COALESCE(u.is_bot, false) AND NOT COALESCE(u.is_test, false)
"""
_BASE = _BASE_SQL.replace("@RANK@", _RANK_ALL)
_BASE_ONE = _BASE_SQL.replace("@RANK@", _RANK_ONE)

_COLUMNS = ("id", "user_id", "status", "created_at", "stage_changed_at", "version", "tags", "university", "course", "vacancy",
            "best_profession", "full_name", "phone", "email", "telegram", "source", "assignee_id", "a_tg", "a_first", "a_last",
            "a_username", "points", "competency", "fit_prof", "fit_score", "place")


def _like(s: str) -> str:
    return "%" + s.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def _comp_sql(key: str) -> str:
    assert key in COMPETENCIES  # whitelisted: interpolated into the SQL text
    return (f"(CASE WHEN jsonb_typeof(c.competency -> '{key}') = 'number' THEN (c.competency ->> '{key}')::numeric ELSE 0 END)")


class Filters:
    """Validated list / pipeline filters -> (WHERE fragments over the subquery `c`, bind params)."""

    def __init__(self, *, q: str | None = None, statuses: list[str] | None = None, profession: str | None = None,
                 university: str | None = None, min_points: int | None = None, min_fit: int | None = None,
                 competency: dict[str, int] | None = None, assignee: str | None = None) -> None:
        self.q = (q or "").strip() or None
        self.statuses = statuses or []
        self.profession = profession
        self.university = university
        self.min_points = min_points
        self.min_fit = min_fit
        self.competency = competency or {}
        self.assignee = assignee

    def sql(self, actor: HrActor) -> tuple[list[str], dict[str, Any]]:
        conds: list[str] = []
        p: dict[str, Any] = {}
        if self.statuses:
            conds.append("c.status = ANY(CAST(:statuses AS text[]))")
            p["statuses"] = self.statuses
        if self.profession:
            conds.append("(c.vacancy = :profession OR c.fit_prof = :profession)")
            p["profession"] = self.profession
        if self.university:
            conds.append("c.university = :university")
            p["university"] = self.university
        if self.min_points:
            conds.append("c.points >= :min_points")
            p["min_points"] = self.min_points
        if self.min_fit:
            conds.append("c.fit_score >= :min_fit")
            p["min_fit"] = self.min_fit
        for key, minimum in self.competency.items():
            conds.append(f"{_comp_sql(key)} >= :comp_{key}")
            p[f"comp_{key}"] = minimum
        if self.assignee == "me":
            conds.append("c.assignee_id = :me")
            p["me"] = actor.id
        elif self.assignee == "none":
            conds.append("c.assignee_id IS NULL")
        if self.q:
            # an analyst must not learn names / contacts through the search box (the oracle would defeat the mask)
            parts = ["c.university ILIKE :qlike ESCAPE '!'", "CAST(c.id AS text) = :qraw"]
            if actor.pii:
                parts += [f"c.{col} ILIKE :qlike ESCAPE '!'" for col in ("full_name", "phone", "email", "telegram")]
            conds.append("(" + " OR ".join(parts) + ")")
            p["qlike"], p["qraw"] = _like(self.q), self.q
        return conds, p


# ---------------------------------------------------------------- cursor

def encode_cursor(sort: str, value: Any, last_id: int) -> str:
    raw = json.dumps([sort, value, last_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, sort: str) -> tuple[Any, int]:
    try:
        sort_c, value, last_id = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if sort_c != sort or isinstance(last_id, bool) or not isinstance(last_id, int):
            raise ValueError("cursor of another sort")
        if sort == "fresh":
            value = datetime.fromisoformat(value)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("bad value")
        return value, last_id
    except (ValueError, TypeError, KeyError):
        raise invalid("cursor", "Invalid cursor (use nextCursor of the same sort)") from None


# (ORDER BY, keyset comparison, value of the sort key in a row)
_SORT = {
    "fresh": ("c.created_at DESC, c.id DESC", "(c.created_at, c.id) < (CAST(:cv AS timestamptz), :cid)"),
    "points": ("c.points DESC, c.id DESC", "(c.points, c.id) < (:cv, :cid)"),
    "fit": ("c.fit_score DESC, c.id DESC", "(c.fit_score, c.id) < (:cv, :cid)"),
    "rank": (f"COALESCE(c.place, {MAX_PLACE}) ASC, c.id ASC", f"(COALESCE(c.place, {MAX_PLACE}), c.id) > (:cv, :cid)"),
}


def _sort_value(sort: str, r: Any) -> Any:
    if sort == "fresh":
        return r.created_at.isoformat()
    return {"points": r.points, "fit": r.fit_score, "rank": r.place if r.place is not None else MAX_PLACE}[sort]


# ---------------------------------------------------------------- row -> contract

def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _obj(v: Any) -> Any:
    """jsonb from a raw `text()` query: the asyncpg dialect hands it over as a string."""
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def _score(competency: Any, key: str) -> int:
    competency = _obj(competency)
    v = competency.get(key) if isinstance(competency, dict) else None
    return max(0, min(100, round(v))) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def staff_name(hr_id: int, tg_id: int | None, first: str | None, last: str | None, username: str | None) -> str:
    """Name of an HR user as `/api/hr/users` shows it (player row, `tg_id` = the player's), `HR #id` without a player row."""
    if tg_id is None:
        return f"HR #{hr_id}"
    return " ".join(p for p in (first, last) if p) or username or f"Player {tg_id}"


def _who(hr: HrUser | None, user: User | None) -> str:
    return "HR" if hr is None else staff_name(hr.id, user.tg_id if user else None, user.first_name if user else None,
                                              user.last_name if user else None, user.username if user else None)


def row_out(r: Any) -> dict[str, Any]:
    """`CandidateRow` of the client (before masking)."""
    role = next((x for x in (r.vacancy, r.fit_prof, r.best_profession) if x in PROFESSION_TITLES), DEFAULT_ROLE)
    return {
        "id": r.id, "fullName": r.full_name, "status": r.status, "university": r.university or "", "course": r.course or "",
        "roleId": role, "source": r.source, "createdAt": ms(r.created_at), "stageChangedAt": ms(r.stage_changed_at),
        "assignee": ({"id": r.assignee_id, "name": staff_name(r.assignee_id, r.a_tg, r.a_first, r.a_last, r.a_username)}
                     if r.assignee_id is not None else None),
        "tags": list(r.tags or []), "achievementPoints": int(r.points),
        "competency": {k: _score(r.competency, k) for k in COMPETENCIES},
        "fitBest": {"profession": r.fit_prof, "score": int(r.fit_score)} if r.fit_prof else None,
        "seasonRank": int(r.place) if r.place is not None else None, "version": r.version,
    }


def _masked_row(r: Any, actor: HrActor) -> dict[str, Any]:
    return mask_pii(row_out(r), actor)


# ---------------------------------------------------------------- queries

class Candidates:
    def __init__(self, sm: async_sessionmaker[AsyncSession], *, bots_enabled: bool = True) -> None:
        self.sm = sm
        self.bots = bots_enabled

    def _wrap(self, where: list[str], tail: str = "", base: str = _BASE) -> str:
        return f"SELECT c.* FROM ({base}) c" + (" WHERE " + " AND ".join(where) if where else "") + tail

    async def _one(self, session: AsyncSession, lead_id: int) -> Any:
        sql = text(self._wrap(["c.id = :lid"], base=_BASE_ONE)).bindparams(bots=self.bots, lid=lead_id)
        row = (await session.execute(sql)).first()
        if row is None:
            raise AppError("lead_not_found", "Candidate not found")
        return row

    async def row(self, session: AsyncSession, actor: HrActor, lead_id: int) -> dict[str, Any]:
        return _masked_row(await self._one(session, lead_id), actor)

    # ---- GET /api/hr/candidates
    async def page(self, actor: HrActor, f: Filters, sort: str, cursor: str | None, limit: int) -> dict[str, Any]:
        conds, params = f.sql(actor)
        params["bots"] = self.bots
        async with self.sm() as session:
            total = (await session.execute(text(f"SELECT count(*) FROM ({self._wrap(conds)}) t"), params)).scalar_one()
            page_conds, page_params = list(conds), dict(params)
            if cursor:
                value, last_id = decode_cursor(cursor, sort)
                page_conds.append(_SORT[sort][1])
                page_params.update(cv=value, cid=last_id)
            sql = self._wrap(page_conds, f" ORDER BY {_SORT[sort][0]} LIMIT :lim")
            rows = (await session.execute(text(sql), {**page_params, "lim": limit + 1})).all()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [_masked_row(r, actor) for r in rows], "total": int(total),
                "nextCursor": encode_cursor(sort, _sort_value(sort, rows[-1]), rows[-1].id) if more and rows else None}

    # ---- GET /api/hr/pipeline
    async def pipeline(self, actor: HrActor, f: Filters) -> dict[str, Any]:
        conds, params = f.sql(actor)
        params["bots"] = self.bots
        async with self.sm() as session:
            counts = dict((await session.execute(
                text(f"SELECT t.status, count(*) FROM ({self._wrap(conds)}) t GROUP BY t.status"), params)).all())
            sql = ("SELECT * FROM (SELECT c.*, row_number() OVER (PARTITION BY c.status ORDER BY c.stage_changed_at DESC, c.id DESC) AS rn "
                   f"FROM ({_BASE}) c" + (" WHERE " + " AND ".join(conds) if conds else "") + ") t WHERE t.rn <= :per "
                   "ORDER BY t.status, t.rn")
            rows = (await session.execute(text(sql), {**params, "per": PIPELINE_ITEMS})).all()
        by_status: dict[str, list[dict[str, Any]]] = {s: [] for s in STATUSES}
        for r in rows:
            by_status[r.status].append(_masked_row(r, actor))
        return {"columns": [{"status": s, "count": int(counts.get(s, 0)), "items": by_status[s]} for s in STATUSES]}

    # ---- GET /api/hr/candidates/{id}
    async def detail(self, actor: HrActor, lead_id: int) -> dict[str, Any]:
        async with self.sm() as session, session.begin():
            r = await self._one(session, lead_id)
            lead = (await session.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
            await log_event(session, lead_id, actor, "view", {"masked": not actor.pii})  # the 152-FZ access log
            user = (await session.get(User, lead.user_id)) if lead.user_id is not None else None
            out = row_out(r)
            out.update({
                "phone": lead.phone, "email": lead.email, "telegram": f"@{lead.telegram}" if lead.telegram else None,
                "birthDate": lead.birth_date.isoformat() if lead.birth_date else None,
                "readyFrom": lead.ready_from.isoformat() if lead.ready_from else None, "practiceType": lead.practice_type,
                "game": await self._game(session, lead),
                "achievements": await self._achievements(session, lead.user_id),
                "fit": await self._fit(session, lead.user_id),
                # beyond the client contract: the fit to every active vacancy (C5), best first; [] for a landing lead
                "vacancyFit": await vacancies.fits_for_user(session, lead.user_id) if lead.user_id is not None else [],
                "history": await self._history(session, lead),
                "notes": await self._notes(session, lead_id) if actor.pii else [],  # free text: never for the analyst
                "canMessage": bool(user is not None and user.bot_started_at is not None),
            })
        return mask_pii(out, actor)

    async def _game(self, session: AsyncSession, lead: Lead) -> dict[str, Any]:
        empty = {"level": 0, "stars": 0, "quality": 0, "shiftsTotal": 0, "quizScore": lead.quiz_score, "byProfession": []}
        if lead.user_id is None:
            return empty
        f = (await session.execute(select(Factory.level, Factory.stars, Factory.quality_avg, Factory.snapshot, Factory.state)
                                   .where(Factory.user_id == lead.user_id))).first()
        correct, answered = await quiz_counts(session, lead.user_id)
        out = {**empty, "quizScore": correct if answered else lead.quiz_score}
        if f is None:
            return out
        skills = next((s for s in ((f.snapshot or {}).get("skills"), (f.state or {}).get("skills")) if isinstance(s, dict)), {})
        by = skills.get("byProfession") if isinstance(skills.get("byProfession"), dict) else {}
        profs = []
        for pid in PROFESSIONS:
            v = by.get(pid)
            played = int(v["played"]) if isinstance(v, dict) and isinstance(v.get("played"), (int, float)) else 0
            if played > 0:
                wins = v.get("wins")
                avg = v.get("avgScore")
                profs.append({"profession": pid, "played": played, "wins": int(wins) if isinstance(wins, (int, float)) else 0,
                              "avgScore": round(float(avg), 2) if isinstance(avg, (int, float)) else 0.0})
        total = skills.get("shiftsTotal")
        out.update(level=int(f.level), stars=int(f.stars), quality=round(float(f.quality_avg) * 100), byProfession=profs,
                   shiftsTotal=int(total) if isinstance(total, (int, float)) else sum(p["played"] for p in profs))
        return out

    async def _achievements(self, session: AsyncSession, user_id: int | None) -> list[dict[str, Any]]:
        earned: dict[str, AchievementEarned] = {}
        progress: dict[str, AchievementProgress] = {}
        if user_id is not None:
            for e in (await session.execute(select(AchievementEarned).where(AchievementEarned.user_id == user_id)
                                            .order_by(AchievementEarned.earned_at))).scalars():
                earned[e.achievement_id] = e  # seasonal ones repeat per season: the latest wins
            for p in (await session.execute(select(AchievementProgress).where(AchievementProgress.user_id == user_id))).scalars():
                if p.achievement_id not in progress or p.season_key > progress[p.achievement_id].season_key:
                    progress[p.achievement_id] = p
        out = []
        for a in catalog.CATALOG:
            e, p = earned.get(a.id), progress.get(a.id)
            entry = a.public()
            entry["earnedAt"] = ms(e.earned_at) if e else None
            entry["evidence"] = evidence_text(a.id, e.evidence) if e else None
            entry["progress"] = (None if e or a.target <= 1 or p is None else
                                 {"current": int(min(p.current, a.target)), "target": int(a.target)})
            out.append(entry)
        return out

    async def _fit(self, session: AsyncSession, user_id: int | None) -> list[dict[str, Any]]:
        if user_id is None:
            return []
        fit = (await session.execute(
            text("SELECT ps.fit FROM factories f JOIN player_scores ps ON ps.user_id = f.user_id AND ps.season_id = f.season_id "
                 "WHERE f.user_id = :u"), {"u": user_id})).scalar_one_or_none()
        out = []
        fit = _obj(fit)
        for pid, v in (fit.items() if isinstance(fit, dict) else []):
            if not isinstance(v, dict) or not isinstance(v.get("score"), (int, float)) or isinstance(v.get("score"), bool):
                continue
            reasons = [{"text": str(x.get("text", "")), "delta": round(x["delta"]) if isinstance(x.get("delta"), (int, float)) else 0}
                       for x in (v.get("reasons") if isinstance(v.get("reasons"), list) else []) if isinstance(x, dict)]
            out.append({"profession": pid, "title": PROFESSION_TITLES.get(pid, pid), "score": round(v["score"]), "reasons": reasons})
        return sorted(out, key=lambda x: (-x["score"], x["profession"]))

    async def _history(self, session: AsyncSession, lead: Lead) -> list[dict[str, Any]]:
        rows = (await session.execute(
            select(LeadEvent, HrUser, User).outerjoin(HrUser, HrUser.id == LeadEvent.actor_hr_id)
            .outerjoin(User, User.tg_id == HrUser.tg_id).where(LeadEvent.lead_id == lead.id)
            .order_by(LeadEvent.created_at.desc(), LeadEvent.id.desc()).limit(HISTORY_LIMIT))).all()
        out = []
        for ev, hr, user in rows:
            who = _who(hr, user) if hr is not None else ("игра" if ev.kind == "achievement" else "система")
            out.append({"id": ev.id, "kind": ev.kind, "at": ms(ev.created_at), "actor": who, "payload": ev.payload or {}})
        if len(rows) < HISTORY_LIMIT:  # the form itself: the first line of the history (not a stored row)
            out.append({"id": -lead.id, "kind": "status", "at": ms(lead.created_at), "actor": "система", "payload": {"to": "new"}})
        return out

    async def _notes(self, session: AsyncSession, lead_id: int) -> list[dict[str, Any]]:
        rows = (await session.execute(
            select(LeadNote, HrUser, User).outerjoin(HrUser, HrUser.id == LeadNote.author_hr_id)
            .outerjoin(User, User.tg_id == HrUser.tg_id).where(LeadNote.lead_id == lead_id)
            .order_by(LeadNote.created_at.desc(), LeadNote.id.desc()).limit(NOTES_LIMIT))).all()
        return [{"id": n.id, "author": _who(hr, u), "text": n.text, "at": ms(n.created_at)} for n, hr, u in rows]

    # ---- PATCH /api/hr/candidates/{id}
    async def patch(self, actor: HrActor, lead_id: int, *, version: int, fields: dict[str, Any]) -> dict[str, Any]:
        """`fields` holds only what the client sent: `status`, `assignee_id` (None = unassign), `tags`."""
        now = datetime.now(timezone.utc)
        async with self.sm() as session, session.begin():
            lead = await self._lock(session, lead_id)
            if lead.version != version:
                raise AppError("version_conflict", "The candidate was changed by someone else; reload the card",
                               details={"version": lead.version})
            changed = False
            if "status" in fields and fields["status"] != lead.status:
                await log_event(session, lead.id, actor, "status", {"from": lead.status, "to": fields["status"], "via": "web"})
                lead.status, lead.stage_changed_at, changed = fields["status"], now, True
            if "assignee_id" in fields and fields["assignee_id"] != lead.assignee_id:
                new = fields["assignee_id"]
                old_name = await self._assignee_name(session, lead.assignee_id)
                new_name = await self._assignee_name(session, new, writer=True) if new is not None else "никто"
                await log_event(session, lead.id, actor, "assign", {"from": old_name, "to": new_name, "fromId": lead.assignee_id, "toId": new})
                lead.assignee_id, changed = new, True
            if "tags" in fields and fields["tags"] != list(lead.tags or []):
                old = list(lead.tags or [])
                await log_event(session, lead.id, actor, "tag", {
                    "tags": fields["tags"], "added": [t for t in fields["tags"] if t not in old],
                    "removed": [t for t in old if t not in fields["tags"]]})
                lead.tags, changed = fields["tags"], True
            if changed:
                lead.version, lead.updated_at = lead.version + 1, now
                await session.flush()
            return await self.row(session, actor, lead_id)

    async def _lock(self, session: AsyncSession, lead_id: int) -> Lead:
        lead = (await session.execute(
            select(Lead).outerjoin(User, User.id == Lead.user_id)
            .where(Lead.id == lead_id, Lead.consent_revoked_at.is_(None), User.is_bot.isnot(True), User.is_test.isnot(True))
            .with_for_update(of=Lead))).scalar_one_or_none()
        if lead is None:
            raise AppError("lead_not_found", "Candidate not found")
        return lead

    async def _assignee_name(self, session: AsyncSession, hr_id: int | None, *, writer: bool = False) -> str:
        if hr_id is None:
            return "никто"
        row = (await session.execute(select(HrUser, User).outerjoin(User, User.tg_id == HrUser.tg_id)
                                     .where(HrUser.id == hr_id))).first()
        if row is None or (writer and (not row[0].active or row[0].role == "analyst")):
            raise invalid("assigneeId", "Unknown, inactive or read-only HR user")
        return _who(*row)

    # ---- POST /api/hr/candidates/{id}/notes
    async def add_note(self, actor: HrActor, lead_id: int, body: str) -> dict[str, Any]:
        async with self.sm() as session, session.begin():
            await self._lock(session, lead_id)
            note = LeadNote(lead_id=lead_id, author_hr_id=actor.id, text=body)
            session.add(note)
            await session.flush()
            await log_event(session, lead_id, actor, "note", {"noteId": note.id})  # no text: analysts read the history
            return {"id": note.id, "author": actor.name, "text": note.text, "at": ms(note.created_at)}

    # ---- POST /api/hr/candidates/{id}/messages
    async def send_message(self, actor: HrActor, lead_id: int, template: str, body: str | None) -> dict[str, Any]:
        async with self.sm() as session, session.begin():
            lead = await self._lock(session, lead_id)
            user = (await session.get(User, lead.user_id)) if lead.user_id is not None else None
            if user is None or user.bot_started_at is None:
                raise AppError("bot_not_started", "The candidate has not started the bot: nobody to write to", status=409)
            outbox_id = (await session.execute(insert(Outbox).values(kind="hr_message", payload={
                "lead_id": lead.id, "user_id": user.id, "tg_id": user.tg_id, "template": template, "text": body,
                "first_name": (lead.full_name.split() or [""])[0] if lead.full_name else "",
                "vacancy": lead.vacancy, "hr_user_id": actor.id}).returning(Outbox.id))).scalar_one()
            await log_event(session, lead_id, actor, "message", {"template": template, "outboxId": outbox_id})
        return {"ok": True, "id": outbox_id}


# ---------------------------------------------------------------- evidence ("what confirms it")

_EVIDENCE = {  # engine evidence key -> (label, formatter)
    "orders": ("заказов", str), "produced": ("выпущено бутылок", str), "loans": ("авансов погашено", str),
    "played": ("смен", str), "wins": ("побед", str), "professions": ("профессий", str), "facts": ("фактов музея", str),
    "invited": ("приглашено", str), "days": ("суток подряд", str), "place": ("место в сезоне", str),
    "shifts": ("смен", str), "skus": ("напитков", lambda v: str(len(v)) if isinstance(v, list) else str(v)),
    "quality": ("качество", lambda v: f"{float(v) * 100:.1f} %".replace(".", ",")),
    "minQuality": ("порог качества", lambda v: f"{float(v) * 100:.0f} %"),
    "avgScore": ("средний балл", lambda v: f"{float(v):.2f}".replace(".", ",")),
    "belowMs": ("реакция быстрее, мс", str), "minScore": ("балл не ниже", lambda v: f"{float(v):.2f}".replace(".", ",")),
}


def evidence_text(achievement_id: str, ev: Any) -> str | None:
    """Evidence jsonb of `achievements_earned` as a short Russian line for the card (the engine stores plain numbers)."""
    if not isinstance(ev, dict):
        return None
    parts = []
    if isinstance(ev.get("correct"), (int, float)) and isinstance(ev.get("answered"), (int, float)):
        parts.append(f"верных ответов: {int(ev['correct'])} из {int(ev['answered'])}")
    for key, value in ev.items():
        if key in ("correct", "answered", "last", "quest", "profession", "season_key") or isinstance(value, (bool, dict)):
            continue
        label, fmt = _EVIDENCE.get(key, (key, str))
        try:
            parts.append(f"{label}: {fmt(value)}")
        except (TypeError, ValueError):
            continue
    return " · ".join(parts) or None
