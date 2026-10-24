"""C5: CSV export of the candidates, `GET /api/hr/export.csv` (hr_lead+).

Same file format as the bot's `/export` (`bot/app/hr/queries.py`): UTF-8 with BOM, `;` separated, CRLF, spreadsheet formulas
neutralised. On top of the anketa columns: achievement points (ОД), the six competencies and the best fit. Never in the file: leads
whose consent was revoked, demo bots, test users. Every export writes one `export` event per exported lead (the 152-FZ access log)
in the same transaction as the read.

WHO is exported is decided by C4's candidate subquery and `candidates.Filters` - the very ones of `GET /api/hr/candidates` - so the
file holds exactly the candidates the HR user sees in the list for the same query string (`sort`, `cursor`, `limit` do not apply).
This module only adds the export columns (the anketa fields the list does not carry) for those ids.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..achievements.catalog import COMPETENCIES, PROFESSIONS
from .access import HrActor
from .candidates import _BASE, Filters

MSK = timezone(timedelta(hours=3))
COMPETENCY_TITLES = {"speed": "скорость", "accuracy": "точность", "attention": "внимательность", "endurance": "выносливость",
                     "reliability": "надёжность", "company": "знание компании"}
INSTITUTION_KINDS = {"vuz": "вуз", "college": "колледж"}


def audit(f: Filters) -> dict[str, Any]:
    """What the export was filtered by, for the event payload: names and non-personal values (the free-text `q` only as a flag)."""
    out: dict[str, Any] = {}
    if f.q:
        out["q"] = True
    if f.statuses:
        out["status"] = list(f.statuses)
    for key, val in (("profession", f.profession), ("university", f.university), ("minPoints", f.min_points),
                     ("minFit", f.min_fit), ("assignee", f.assignee)):
        if val:
            out[key] = val
    if f.competency:
        out["competency"] = dict(f.competency)
    return out


# the export columns of the candidates `Filters` selected; the score row is the one of the player's current season (as in the list)
_SQL = """
SELECT l.id, l.created_at, l.status, l.full_name, l.birth_date, l.phone, l.email, l.telegram, l.university, l.institution_kind,
       l.direction, l.course, l.vacancy, l.readiness, l.ready_from, l.practice_type, l.tags, l.best_profession, l.season_place,
       l.quiz_score, l.season_id, l.user_id, coalesce(nullif(l.source, ''), nullif(u.source, '')) AS source,
       ps.achievement_points, ps.competency, ps.fit
FROM leads l LEFT JOIN users u ON u.id = l.user_id
LEFT JOIN LATERAL (SELECT s.achievement_points, s.competency, s.fit FROM factories f
                   JOIN player_scores s ON s.user_id = f.user_id AND s.season_id = f.season_id
                   WHERE f.user_id = l.user_id) ps ON true
WHERE l.id IN (SELECT c.id FROM (@BASE@) c @WHERE@)
ORDER BY l.created_at DESC, l.id DESC
"""


async def fetch_rows(session: AsyncSession, f: Filters, actor: HrActor, *, bots_enabled: bool = True) -> list[dict[str, Any]]:
    conds, params = f.sql(actor)
    sql = _SQL.replace("@BASE@", _BASE).replace("@WHERE@", ("WHERE " + " AND ".join(conds)) if conds else "")
    return [dict(r) for r in (await session.execute(text(sql), {**params, "bots": bots_enabled})).mappings().all()]


async def log_export(session: AsyncSession, rows: list[dict[str, Any]], actor: HrActor, f: Filters) -> None:
    """One `export` event per exported lead, in the caller's transaction."""
    if not rows:
        return
    await session.execute(
        text("INSERT INTO lead_events (lead_id, actor_hr_id, kind, payload) "
             "SELECT x, :actor, 'export', CAST(:payload AS jsonb) FROM unnest(CAST(:ids AS bigint[])) AS x"),
        {"actor": actor.id, "ids": [r["id"] for r in rows],
         "payload": json.dumps({"format": "csv", "count": len(rows), "filters": audit(f)})})


# ---------------------------------------------------------------- the file

_PHONE_RE = re.compile(r"^\+?\d[\d ()-]*$")
COLUMNS = (
    "id", "создана (МСК)", "статус", "ФИО", "дата рождения", "возраст", "телефон", "email", "telegram", "вуз", "вид учреждения",
    "направление", "курс", "вакансия", "готовность", "готов с", "тип практики", "теги", "лучшая профессия", "место в сезоне",
    "квиз (верных из 8)", "источник", "сезон", "ОД",
    *(f"компетенция: {COMPETENCY_TITLES[k]}" for k in COMPETENCIES), "лучший fit: профессия", "лучший fit: балл",
)


def csv_safe(v: str) -> str:
    """A cell starting with = + - @ (or a control char) gets a leading apostrophe; phone-looking values are left alone."""
    if v and v[0] in "=+-@\t\r" and not _PHONE_RE.match(v):
        return "'" + v
    return v


def _age(born: date | None, today: date) -> str:
    if born is None:
        return ""
    return str(today.year - born.year - ((today.month, today.day) < (born.month, born.day)))


def best_fit(fit: Mapping[str, Any] | None) -> tuple[str, str]:
    best: tuple[str, float] | None = None
    for prof, v in (fit or {}).items():
        score = v.get("score") if isinstance(v, Mapping) else v
        if isinstance(score, (int, float)) and (best is None or score > best[1]):
            best = (prof, score)
    return (best[0], str(int(round(best[1])))) if best else ("", "")


def row_cells(r: Mapping[str, Any], today: date) -> list[str]:
    s = lambda v: "" if v is None else csv_safe(str(v))  # noqa: E731
    comp = r.get("competency") or {}
    has_player = r.get("user_id") is not None
    points = r.get("achievement_points")
    prof, score = best_fit(r.get("fit"))
    return [
        str(r["id"]), r["created_at"].astimezone(MSK).strftime("%Y-%m-%d %H:%M"), s(r["status"]), s(r["full_name"]),
        r["birth_date"].isoformat() if r.get("birth_date") else "", _age(r.get("birth_date"), today), s(r.get("phone")),
        s(r.get("email")), s(r.get("telegram")), s(r.get("university")),
        s(INSTITUTION_KINDS.get(r.get("institution_kind") or "", r.get("institution_kind"))), s(r.get("direction")), s(r.get("course")),
        s(PROFESSIONS.get(r.get("vacancy") or "", r.get("vacancy"))), s(r.get("readiness")),
        r["ready_from"].isoformat() if r.get("ready_from") else "", s(r.get("practice_type")), ",".join(r.get("tags") or []),
        s(PROFESSIONS.get(r.get("best_profession") or "", r.get("best_profession"))), s(r.get("season_place")),
        s(r.get("quiz_score")), s(r.get("source")), s(r.get("season_id")),
        str(int(points)) if points is not None else ("0" if has_player else ""),
        *(str(int(round(float(comp[k])))) if isinstance(comp.get(k), (int, float)) else "" for k in COMPETENCIES),
        s(PROFESSIONS.get(prof, prof)), score,
    ]


def build_csv(rows: list[dict[str, Any]], today: date | None = None) -> bytes:
    today = today or datetime.now(MSK).date()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow(row_cells(r, today))
    return buf.getvalue().encode("utf-8-sig")


def filename(now: datetime | None = None) -> str:
    return f"candidates-{(now or datetime.now(MSK)).astimezone(MSK):%Y-%m-%d}.csv"
