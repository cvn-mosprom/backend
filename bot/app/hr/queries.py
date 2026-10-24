"""Read-only HR analytics queries (plus the lead status update) as plain SQL over the api's tables.

Rules shared by every query: demo bots (``users.is_bot``) and test users (``users.is_test``, S13) are excluded; a lead whose consent was revoked
(``consent_revoked_at``) is excluded from the active views and the export. The current season is the newest row of
``seasons`` (the same definition the api uses). Nothing here reads ``factories.state``.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .format import INSTITUTION_KINDS, MSK, PROFESSIONS, QUIZ_QUESTION_IDS, STATUS_ALIASES, STATUSES, TAGS, age_years

# Funnel steps = the subset of api/app/events.py EVENT_NAMES that marks progress (a test keeps them in sync).
FUNNEL_STEPS: tuple[tuple[str, str], ...] = (
    ("app_open", "Открыли игру"),
    ("session_start", "Начали сессию (интро)"),
    ("game_start", "Запустили смену"),
    ("game_end", "Завершили смену"),
    ("finale_view", "Дошли до финала сезона"),
    ("lead_form_open", "Открыли анкету"),
    ("lead_submit", "Отправили анкету"),
)

LEAD_COLS = """l.id, l.user_id, l.season_id, l.full_name, l.birth_date, l.phone, l.email, l.telegram, l.university,
    l.institution_kind, l.direction, l.course, l.vacancy, l.ready_from, l.readiness, l.practice_type, l.tags, l.status, l.hr_note,
    l.best_profession, l.season_place, l.quiz_score, l.consent_revoked_at, l.created_at, l.updated_at,
    u.tg_id, u.username AS tg_username, u.source"""
LEAD_FROM = "FROM leads l JOIN users u ON u.id = l.user_id"
PAGE_SIZE = 10


@dataclass(frozen=True)
class LeadFilter:
    status: str | None = None
    university: str | None = None
    tag: str | None = None
    include_revoked: bool = False

    def title(self) -> str:
        bits = ["Анкеты"]
        if self.status:
            bits.append(self.status)
        if self.university:
            bits.append(f"вуз «{self.university}»")
        if self.tag:
            bits.append("#" + self.tag)
        if self.include_revoked:
            bits.append("с отозванными")
        return " · ".join(bits)


def parse_filter(args: str | None) -> LeadFilter:
    """``/leads [status] [uni] [tag] [all]``: tokens in any order. A status word (en / ru), a tag (``#senior`` or the
    bare name) and ``all`` are recognised; whatever is left is the university (substring, case-insensitive)."""
    status = tag = None
    include_revoked = False
    rest: list[str] = []
    for tok in (args or "").split():
        low = tok.lower().lstrip("#")
        if low == "all" or low == "все":
            include_revoked = True
        elif low in STATUSES and status is None:
            status = low
        elif low in STATUS_ALIASES and status is None:
            status = STATUS_ALIASES[low]
        elif low in TAGS and tag is None:
            tag = low
        else:
            rest.append(tok)
    return LeadFilter(status=status, university=" ".join(rest) or None, tag=tag, include_revoked=include_revoked)


def like_pattern(s: str) -> str:
    return "%" + re.sub(r"([!%_])", r"!\1", s.strip()) + "%"


def _lead_where(f: LeadFilter, *, revoked_ok: bool) -> tuple[str, dict[str, Any]]:
    conds, params = ["u.is_bot = false AND u.is_test = false"], {}
    if not (revoked_ok and f.include_revoked):
        conds.append("l.consent_revoked_at IS NULL")
    if f.status:
        conds.append("l.status = :status")
        params["status"] = f.status
    if f.university:
        conds.append("l.university ILIKE :uni ESCAPE '!'")
        params["uni"] = like_pattern(f.university)
    if f.tag:
        conds.append("CAST(:tag AS text) = ANY(l.tags)")
        params["tag"] = f.tag
    return " AND ".join(conds), params


async def fetch_lead(s: AsyncSession, lead_id: int) -> dict[str, Any] | None:
    row = (await s.execute(text(f"SELECT {LEAD_COLS} {LEAD_FROM} WHERE l.id = :id"), {"id": lead_id})).mappings().first()
    return dict(row) if row else None


async def list_leads(s: AsyncSession, f: LeadFilter, page: int = 0, size: int = PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
    """Newest first. Returns (rows of the page, total matching)."""
    where, params = _lead_where(f, revoked_ok=True)
    total = (await s.execute(text(f"SELECT count(*) {LEAD_FROM} WHERE {where}"), params)).scalar_one()
    rows = (await s.execute(
        text(f"SELECT {LEAD_COLS} {LEAD_FROM} WHERE {where} ORDER BY l.created_at DESC, l.id DESC LIMIT :n OFFSET :o"),
        {**params, "n": size, "o": max(page, 0) * size})).mappings().all()
    return [dict(r) for r in rows], int(total)


# ---------------------------------------------------------------- status buttons

def actor_label(tg_id: int, username: str | None, first_name: str | None) -> str:
    name = f"@{username}" if username else (first_name or "HR")
    return f"{name} (tg {tg_id})"


async def set_lead_status(s: AsyncSession, lead_id: int, status: str, actor: str,
                          now: datetime | None = None) -> tuple[str, dict[str, Any] | None]:
    """Change a lead's status and append an audit line to ``hr_note``. Runs in its own transaction.

    Returns (outcome, lead card): ``ok`` | ``unchanged`` | ``revoked`` (refused: consent revoked, nothing written) |
    ``not_found`` | ``bad_status``."""
    if status not in STATUSES:
        return "bad_status", None
    now = now or datetime.now(timezone.utc)
    async with s.begin():
        cur = (await s.execute(text("SELECT status, consent_revoked_at FROM leads WHERE id = :id FOR UPDATE"),
                               {"id": lead_id})).first()
        if cur is None:
            return "not_found", None
        if cur.consent_revoked_at is not None:
            outcome = "revoked"
        elif cur.status == status:
            outcome = "unchanged"
        else:
            line = f"{now:%Y-%m-%d %H:%M} UTC · {actor}: {cur.status} → {status}"
            await s.execute(text("UPDATE leads SET status = :st, updated_at = :now, "
                                 "hr_note = concat_ws(E'\\n', hr_note, CAST(:line AS text)) WHERE id = :id"),
                            {"st": status, "now": now, "line": line, "id": lead_id})
            outcome = "ok"
    return outcome, await fetch_lead(s, lead_id)


# ---------------------------------------------------------------- CSV export

CSV_COLUMNS = (
    ("id", "id"), ("created_at", "создана (МСК)"), ("status", "статус"), ("full_name", "ФИО"),
    ("birth_date", "дата рождения"), ("age", "возраст"), ("phone", "телефон"), ("email", "email"),
    ("telegram", "telegram"), ("university", "вуз"), ("institution_kind", "вид учреждения"),
    ("direction", "направление"), ("course", "курс"),
    ("vacancy", "вакансия"), ("readiness", "готовность"), ("ready_from", "готов с"), ("practice_type", "тип практики"),
    ("tags", "теги"), ("best_profession", "лучшая профессия"), ("season_place", "место в сезоне"),
    ("quiz_score", "квиз (верных из 8)"), ("source", "источник"), ("season_id", "сезон"), ("hr_note", "заметки HR"),
)
_PHONE_RE = re.compile(r"^\+?\d[\d ()-]*$")


def csv_safe(v: str) -> str:
    """Neutralise spreadsheet formulas: a cell starting with = + - @ (or a control char) gets a leading apostrophe.
    Phone-looking values are left alone (they legitimately start with +)."""
    if v and v[0] in "=+-@\t\r" and not _PHONE_RE.match(v):
        return "'" + v
    return v


def _cell(key: str, row: dict[str, Any], today: date) -> str:
    if key == "age":
        a = age_years(row.get("birth_date"), today)
        return "" if a is None else str(a)
    v = row.get(key)
    if v is None:
        return ""
    if key == "created_at":
        return v.astimezone(MSK).strftime("%Y-%m-%d %H:%M")
    if key in ("birth_date", "ready_from"):
        return v.isoformat()
    if key == "tags":
        return ",".join(v)
    if key in ("vacancy", "best_profession"):
        return csv_safe(PROFESSIONS.get(v, v))
    if key == "institution_kind":
        return csv_safe(INSTITUTION_KINDS.get(v, v))
    return csv_safe(str(v))


def build_csv(rows: list[dict[str, Any]], today: date | None = None) -> bytes:
    """UTF-8 with BOM (Excel), ``;`` separated (the Russian-locale Excel default), CRLF lines."""
    today = today or datetime.now(MSK).date()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow([title for _, title in CSV_COLUMNS])
    for r in rows:
        w.writerow([_cell(k, r, today) for k, _ in CSV_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


async def export_rows(s: AsyncSession, f: LeadFilter) -> list[dict[str, Any]]:
    """Every lead matching the filter, never revoked ones, never demo bots."""
    where, params = _lead_where(f, revoked_ok=False)
    rows = (await s.execute(text(f"SELECT {LEAD_COLS} {LEAD_FROM} WHERE {where} ORDER BY l.created_at DESC, l.id DESC"),
                            params)).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- analytics

async def current_season(s: AsyncSession) -> dict[str, Any] | None:
    row = (await s.execute(text("SELECT id, starts_at, ends_at, closed FROM seasons ORDER BY id DESC LIMIT 1"))).mappings().first()
    return dict(row) if row else None


async def funnel(s: AsyncSession, season: dict[str, Any]) -> dict[str, Any]:
    """Distinct players per funnel event since the season started, plus table-side counts of the same season."""
    names = [n for n, _ in FUNNEL_STEPS]
    ev = (await s.execute(text(
        "SELECT e.name, count(DISTINCT e.user_id) AS n FROM events e JOIN users u ON u.id = e.user_id "
        "WHERE u.is_bot = false AND u.is_test = false AND e.name = ANY(CAST(:names AS text[])) AND e.ts >= :since GROUP BY e.name"),
        {"names": names, "since": season["starts_at"]})).all()
    by = {r.name: int(r.n) for r in ev}
    tbl = (await s.execute(text("""
        SELECT
          (SELECT count(*) FROM factories f JOIN users u ON u.id = f.user_id WHERE f.season_id = :sid AND u.is_bot = false AND u.is_test = false) AS players,
          (SELECT count(*) FROM shifts sh JOIN users u ON u.id = sh.user_id WHERE sh.season_id = :sid AND u.is_bot = false AND u.is_test = false) AS shifts,
          (SELECT count(DISTINCT sh.user_id) FROM shifts sh JOIN users u ON u.id = sh.user_id WHERE sh.season_id = :sid AND u.is_bot = false AND u.is_test = false) AS shift_players,
          (SELECT count(*) FROM leads l JOIN users u ON u.id = l.user_id WHERE l.season_id = :sid AND u.is_bot = false AND u.is_test = false AND l.consent_revoked_at IS NULL) AS leads,
          (SELECT count(*) FROM leads l JOIN users u ON u.id = l.user_id WHERE l.season_id = :sid AND u.is_bot = false AND u.is_test = false AND l.consent_revoked_at IS NOT NULL) AS revoked
        """), {"sid": season["id"]})).mappings().one()
    return {"steps": [(n, label, by.get(n, 0)) for n, label in FUNNEL_STEPS], "tables": dict(tbl)}


async def sources(s: AsyncSession, limit: int = 30) -> list[dict[str, Any]]:
    rows = (await s.execute(text("""
        SELECT coalesce(nullif(u.source, ''), '(без метки)') AS src, count(*) AS users,
               count(*) FILTER (WHERE EXISTS (SELECT 1 FROM leads l WHERE l.user_id = u.id AND l.consent_revoked_at IS NULL)) AS leads
        FROM users u WHERE u.is_bot = false AND u.is_test = false GROUP BY 1 ORDER BY users DESC, src LIMIT :n"""), {"n": limit})).mappings().all()
    return [dict(r) for r in rows]


def _today_start(now: datetime | None = None) -> datetime:
    n = (now or datetime.now(timezone.utc)).astimezone(MSK)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


async def stats(s: AsyncSession, quiz_total: int, now: datetime | None = None,
                quiz_ids: tuple[str, ...] = QUIZ_QUESTION_IDS) -> dict[str, Any]:
    since = _today_start(now)
    row = (await s.execute(text("""
        SELECT
          (SELECT count(*) FROM users WHERE is_bot = false AND is_test = false) AS users,
          (SELECT count(*) FROM users WHERE is_bot = false AND is_test = false AND created_at >= :since) AS new_today,
          (SELECT count(*) FROM users WHERE is_bot = false AND is_test = false AND last_seen_at >= :since) AS dau,
          (SELECT count(*) FROM factories f JOIN users u ON u.id = f.user_id WHERE u.is_bot = false AND u.is_test = false) AS factories,
          (SELECT count(*) FROM shifts sh JOIN users u ON u.id = sh.user_id WHERE u.is_bot = false AND u.is_test = false AND sh.created_at >= :since) AS shifts_today,
          (SELECT count(DISTINCT qa.user_id) FROM quiz_answers qa JOIN users u ON u.id = qa.user_id
             WHERE u.is_bot = false AND u.is_test = false AND qa.question_id = ANY(CAST(:ids AS text[]))) AS quiz_started
        """), {"since": since, "ids": list(quiz_ids)})).mappings().one()
    out = dict(row)
    quiz = (await s.execute(text("""
        SELECT count(*) AS completed, coalesce(avg(sc), 0) AS avg_score FROM (
          SELECT qa.user_id, count(*) FILTER (WHERE qa.correct) AS sc, count(*) AS n
          FROM quiz_answers qa JOIN users u ON u.id = qa.user_id
          WHERE u.is_bot = false AND u.is_test = false AND qa.question_id = ANY(CAST(:ids AS text[])) GROUP BY qa.user_id) t
        WHERE n >= :total"""), {"total": quiz_total, "ids": list(quiz_ids)})).mappings().one()
    out["quiz_completed"], out["quiz_avg"] = int(quiz["completed"]), float(quiz["avg_score"])
    st = (await s.execute(text(
        "SELECT l.status, count(*) AS n FROM leads l JOIN users u ON u.id = l.user_id "
        "WHERE u.is_bot = false AND u.is_test = false AND l.consent_revoked_at IS NULL GROUP BY l.status"))).all()
    out["leads"] = {r.status: int(r.n) for r in st}
    out["leads_revoked"] = (await s.execute(text(
        "SELECT count(*) FROM leads l JOIN users u ON u.id = l.user_id "
        "WHERE u.is_bot = false AND u.is_test = false AND l.consent_revoked_at IS NOT NULL"))).scalar_one()
    return out


async def top(s: AsyncSession, season_id: int, university: str | None, include_bots: bool, limit: int = 10) -> list[dict[str, Any]]:
    params = {"sid": season_id, "n": limit, "include_bots": include_bots,
              "uni": like_pattern(university) if university else None}
    rows = (await s.execute(text(
        "SELECT f.user_id, f.name, f.stars, f.level, f.university, (f.is_bot OR u.is_bot) AS is_bot, u.username, u.first_name "
        "FROM factories f JOIN users u ON u.id = f.user_id WHERE f.season_id = :sid "
        "AND NOT (f.is_test OR u.is_test) AND (CAST(:include_bots AS boolean) OR NOT (f.is_bot OR u.is_bot)) "
        "AND (CAST(:uni AS text) IS NULL OR f.university ILIKE :uni ESCAPE '!') "
        "ORDER BY f.stars DESC, f.user_id ASC LIMIT :n"), params)).mappings().all()
    return [dict(r) for r in rows]
