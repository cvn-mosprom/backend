"""Leads (S5): the candidate form of the case, 18+ check, consent (152-FZ), automatic priority tags, outbox.

Independent of the sim: only Postgres is touched (the factory snapshot columns are READ for the enrichment).
Personal data (name, birth date, contacts) is never logged: the access log carries no bodies or queries and the
engine hides SQL parameters in error messages (app/db.py `hide_parameters`).

Wire format: request fields are camelCase or snake_case (fullName / full_name ...). The legacy frontend form
(fullName, contact, university, course, roleId, consent) is accepted: `contact` is mapped to phone / email / telegram by
its format, `roleId` is the vacancy. Everything beyond that (birthDate, phone, email, telegram, direction, vacancy,
readyFrom, readiness, practiceType, consentTextVersion) is the case form proper.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, StringConstraints, StrictBool
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import quiz_bank, seasons
from .bodies import invalid
from .errors import AppError
from .leaderboard import best_profession, rank_of
from .models import Factory, Lead, Outbox, QuizAnswer, Season, User
from .schemas import CamelModel

log = logging.getLogger("mosprom.leads")

MIN_AGE = 18
VACANCIES = ("kvasovar", "operator", "adjuster", "lab", "storekeeper", "forklift", "kipia")  # content ProfessionId
READY_WINDOW_DAYS = 120  # `readyFrom` within this many days counts as "ready for the season"
SENIOR_COURSE = 4

TAG_SENIOR = "senior"  # 4th course and above, or a graduate
TAG_PROFILE = "profile_direction"  # direction relevant for a beverage plant
TAG_READY = "ready_for_season"
TAG_QUIZ = "high_quiz"

# Directions relevant for a beverage plant: id -> lowercase keywords searched in the free-text `direction`.
PROFILE_DIRECTIONS: dict[str, tuple[str, ...]] = {
    "food_tech": ("пищев", "food", "напитк", "бродил", "хлебопек", "продовольств", "питани", "агро", "технологическ", "технолог-"),
    "chemistry": ("хим", "chem"),
    "biotech": ("биотех", "биолог", "микробиолог", "biotech"),
    "mechanics": ("механик", "машиностро", "оборудован", "слесар", "мехатрон", "наладч", "станк"),
    "automation": ("автоматиз", "кипиа", "робот", "электроник", "мехатрон"),
    "electrical": ("электр", "энергет"),
    "logistics": ("логист", "складск", "снабжен"),
    "quality": ("качеств", "метролог", "стандартиз", "сертифик", "товаровед", "лаборатор"),
}


def _no_nul(v: str) -> str:
    if "\x00" in v:
        raise ValueError("NUL characters are not allowed")  # Postgres text cannot store them
    return v


def _str(n: int, min_length: int = 0):
    """Trimmed string, at most n chars (empty after trimming is handled by the service: it means "not given")."""
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=n), AfterValidator(_no_nul)]


S32, S40, S64, S80, S120 = _str(32), _str(40), _str(64), _str(80), _str(120)


class LeadIn(CamelModel):
    full_name: _str(120, 2)
    birth_date: S32 | None = None  # YYYY-MM-DD
    phone: S40 | None = None
    email: S120 | None = None
    telegram: S80 | None = None
    contact: S120 | None = None  # legacy single field: classified by format
    university: _str(200, 1)
    direction: S120 | None = None
    course: int | S40 | None = None
    vacancy: S64 | None = None  # profession id
    role_id: S64 | None = None  # legacy name of `vacancy`
    ready_from: S32 | None = None  # YYYY-MM-DD
    readiness: Literal["now", "season", "later"] | None = None
    practice_type: Literal["paid", "free", "job"] | None = None
    consent: StrictBool | None = None  # anything but true -> 403 consent_required
    consent_text_version: S64 | None = None


# ---------------------------------------------------------------- parsing helpers (pure)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]{2,}$")
_TG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s\-().]{10,25}$")
_TG_PREFIXES = ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "telegram.me/")


def normalize_email(s: str) -> str | None:
    return s.strip().lower() if _EMAIL.match(s.strip()) else None


def normalize_telegram(s: str) -> str | None:
    """Bare username (no '@', no t.me link), 5..32 chars like Telegram's own rule."""
    s = s.strip()
    for p in _TG_PREFIXES:
        if s.lower().startswith(p):
            s = s[len(p):]
            break
    s = s.lstrip("@").rstrip("/")
    return s if _TG.match(s) else None


def normalize_phone(s: str) -> str | None:
    """+7XXXXXXXXXX for Russian numbers (8XXXXXXXXXX, 7XXXXXXXXXX, 10 digits), +<digits> for other 11..15 digit numbers."""
    s = s.strip()
    if not _PHONE_CHARS.match(s):
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    if 11 <= len(digits) <= 15 and s.startswith("+"):
        return "+" + digits
    return None


def classify_contact(raw: str) -> tuple[str, str] | None:
    """Legacy `contact` -> (column, normalized value): email by '@x.y', phone by digits, telegram by username shape."""
    if (e := normalize_email(raw)) is not None:
        return "email", e
    if (p := normalize_phone(raw)) is not None:
        return "phone", p
    if (t := normalize_telegram(raw)) is not None:
        return "telegram", t
    return None


def parse_course(raw: int | str | None) -> tuple[str | None, int | None, bool]:
    """(label, course number, graduate). Free text is kept as given; only digits / 'выпускник' carry meaning."""
    if raw is None or isinstance(raw, bool):
        return None, None, False
    if isinstance(raw, int):
        return str(raw), raw, False
    text = raw.strip().lower()
    if not text:
        return None, None, False
    if any(w in text for w in ("выпуск", "graduate", "аспирант", "окончил")):
        return raw.strip(), None, True
    m = re.search(r"\d+", text)
    return raw.strip(), (int(m.group()) if m else None), False


def parse_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise invalid(field, "Expected a date as YYYY-MM-DD") from None


def age_on(birth: date, today: date) -> int:
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def is_profile_direction(direction: str | None) -> bool:
    d = (direction or "").lower().replace("ё", "е")
    return bool(d) and (d in PROFILE_DIRECTIONS or any(k in d for kws in PROFILE_DIRECTIONS.values() for k in kws))


def high_quiz(correct: int) -> bool:
    return correct / quiz_bank.TOTAL >= quiz_bank.HIGH_SCORE


def today() -> date:
    return datetime.now(timezone.utc).date()  # tests patch this


@dataclass
class Clean:
    full_name: str
    birth_date: date | None
    phone: str | None
    email: str | None
    telegram: str | None
    university: str
    direction: str | None
    course: str | None
    course_num: int | None
    graduate: bool
    vacancy: str | None
    ready_from: date | None
    readiness: str | None
    practice_type: str | None
    consent_version: str | None


def _blank(v: str | None) -> str | None:
    return v.strip() or None if v is not None else None


def clean(body: LeadIn, on: date) -> Clean:
    """Field-level validation and normalisation. Raises 422 validation_error; consent / age are checked by the caller."""
    phone, email, telegram = _blank(body.phone), _blank(body.email), _blank(body.telegram)
    if phone is not None and (phone := normalize_phone(phone)) is None:
        raise invalid("phone", "Not a phone number")
    if email is not None and (email := normalize_email(email)) is None:
        raise invalid("email", "Not an e-mail address")
    if telegram is not None and (telegram := normalize_telegram(telegram)) is None:
        raise invalid("telegram", "Telegram username must be 5..32 letters, digits or underscores")
    if (raw := _blank(body.contact)) is not None:
        found = classify_contact(raw)
        if found is None:
            raise invalid("contact", "Neither a phone, an e-mail nor a Telegram username")
        col, value = found
        if col == "phone" and phone is None:
            phone = value
        elif col == "email" and email is None:
            email = value
        elif col == "telegram" and telegram is None:
            telegram = value
    if not (phone or email or telegram):
        raise invalid("contact", "At least one contact (phone, email or telegram) is required")

    birth = None
    if (raw := _blank(body.birth_date)) is not None:
        birth = parse_date(raw, "birthDate")
        if birth > on or birth.year < 1920:
            raise invalid("birthDate", "Implausible birth date")
    ready_from = parse_date(raw, "readyFrom") if (raw := _blank(body.ready_from)) is not None else None
    vacancy = _blank(body.vacancy) or _blank(body.role_id)
    if vacancy is not None and vacancy not in VACANCIES:
        raise invalid("vacancy", f"Unknown vacancy, expected one of {', '.join(VACANCIES)}")
    label, num, graduate = parse_course(body.course)
    if num is not None and not 1 <= num <= 10:
        raise invalid("course", "Course must be 1..10")
    return Clean(
        full_name=" ".join(body.full_name.split()), birth_date=birth, phone=phone, email=email, telegram=telegram,
        university=body.university, direction=_blank(body.direction), course=label, course_num=num, graduate=graduate,
        vacancy=vacancy, ready_from=ready_from, readiness=body.readiness, practice_type=body.practice_type,
        consent_version=_blank(body.consent_text_version))


def compute_tags(c: Clean, quiz_correct: int, on: date) -> list[str]:
    tags = []
    if c.graduate or (c.course_num is not None and c.course_num >= SENIOR_COURSE):
        tags.append(TAG_SENIOR)
    if is_profile_direction(c.direction):
        tags.append(TAG_PROFILE)
    if c.readiness in ("now", "season") or (c.ready_from is not None and (c.ready_from - on).days <= READY_WINDOW_DAYS):
        tags.append(TAG_READY)
    if high_quiz(quiz_correct):
        tags.append(TAG_QUIZ)
    return tags


# ---------------------------------------------------------------- persistence helpers

def iso(v: date | datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


async def enqueue(session: AsyncSession, kind: str, payload: dict[str, Any]) -> None:
    """Outbox row in the CALLER's transaction (the bot delivers it; a down bot never fails the request)."""
    await session.execute(insert(Outbox).values(kind=kind, payload=payload))


async def quiz_counts(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """(correct, answered) of the player's quiz."""
    row = (await session.execute(select(func.count().filter(QuizAnswer.correct.is_(True)), func.count())
                                 .where(QuizAnswer.user_id == user_id))).one()
    return row[0], row[1]


async def lead_season_id(session: AsyncSession, bots_enabled: bool = True) -> int:
    """Season a new lead belongs to: the one factory writes belong to (app/seasons.py: the newest season, rotated inline
    when overdue; season 1 is created when there is none). DB only."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return (await seasons.ensure(session, now_ms, bots_enabled=bots_enabled)).id


def best_from_skills(skills: Any) -> str | None:
    by = skills.get("byProfession") if isinstance(skills, dict) else None
    return best_profession(skills)[0] if isinstance(by, dict) and by else None


def lead_out(l: Lead) -> dict[str, Any]:
    """The owner's view of a lead (no hr_note: HR's private comment)."""
    return {
        "id": l.id, "seasonId": l.season_id, "status": l.status, "tags": list(l.tags or []),
        "fullName": l.full_name, "birthDate": iso(l.birth_date), "phone": l.phone, "email": l.email,
        "telegram": l.telegram, "university": l.university, "direction": l.direction, "course": l.course,
        "vacancy": l.vacancy, "readyFrom": iso(l.ready_from), "readiness": l.readiness, "practiceType": l.practice_type,
        "consent": l.consent, "consentAt": iso(l.consent_at), "consentTextVersion": l.consent_text_version,
        "consentRevokedAt": iso(l.consent_revoked_at), "bestProfession": l.best_profession,
        "seasonPlace": l.season_place, "quizScore": l.quiz_score, "createdAt": iso(l.created_at),
        "updatedAt": iso(l.updated_at),
    }


async def lead_status(session: AsyncSession, user_id: int) -> str | None:
    """Value of `leadStatus` in the season summary: the latest lead's status, 'revoked' after a consent revocation."""
    row = (await session.execute(select(Lead.status, Lead.consent_revoked_at).where(Lead.user_id == user_id)
                                 .order_by(Lead.created_at.desc(), Lead.id.desc()).limit(1))).one_or_none()
    if row is None:
        return None
    return "revoked" if row.consent_revoked_at is not None else row.status


class LeadService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None, *, bots_enabled: bool = True) -> None:
        if sessionmaker is None:
            raise AppError("config_error", "DATABASE_URL is not configured")
        self.sm = sessionmaker
        self.bots_enabled = bots_enabled

    async def create(self, user: User, body: LeadIn) -> dict[str, Any]:
        on = today()
        c = clean(body, on)  # 422
        if body.consent is not True or c.consent_version is None:
            raise AppError("consent_required", "Consent to personal data processing is required",
                           details={"requires": ["consent=true", "consent_text_version"]})
        now = datetime.now(timezone.utc)
        async with self.sm() as session, session.begin():
            f = (await session.execute(
                select(Factory.season_id, Factory.stars,
                       Factory.snapshot["skills"].label("skills"),
                       Factory.snapshot[("player", "ageConfirmed")].as_boolean().label("age_ok"))
                .where(Factory.user_id == user.id))).one_or_none()
            age_confirmed = bool(user.age_confirmed or (f is not None and f.age_ok))
            if c.birth_date is not None:
                if age_on(c.birth_date, on) < MIN_AGE:
                    raise AppError("age_required", f"You must be at least {MIN_AGE} years old",
                                   details={"min_age": MIN_AGE})
            elif not age_confirmed:
                raise AppError("age_required", "Birth date or age confirmation is required",
                               details={"min_age": MIN_AGE})
            season_id = await lead_season_id(session, self.bots_enabled)
            correct, answered = await quiz_counts(session, user.id)
            best = best_from_skills(f.skills) if f is not None else None
            place = None
            if f is not None:
                place, _ = await rank_of(session, f.season_id, user.id, bots_enabled=self.bots_enabled, mine=f.stars)
            tags = compute_tags(c, correct, on)
            values = dict(
                user_id=user.id, season_id=season_id, full_name=c.full_name, birth_date=c.birth_date, phone=c.phone,
                email=c.email, telegram=c.telegram, university=c.university, direction=c.direction, course=c.course,
                vacancy=c.vacancy, ready_from=c.ready_from, readiness=c.readiness, practice_type=c.practice_type,
                consent=True, consent_at=now, consent_text_version=c.consent_version, tags=tags,
                best_profession=best, season_place=place, quiz_score=correct if answered else None)
            lead = (await session.execute(
                pg_insert(Lead).values(**values).on_conflict_do_nothing(constraint="uq_leads_user_season").returning(Lead)
            )).scalar_one_or_none()
            if lead is None:
                revoked = (await session.execute(select(Lead.consent_revoked_at).where(
                    Lead.user_id == user.id, Lead.season_id == season_id))).scalar_one_or_none() is not None
                raise AppError("lead_already_exists", "A lead for this season already exists",
                               details={"season_id": season_id, "revoked": revoked})
            await enqueue(session, "lead_created", {
                "lead_id": lead.id, "user_id": user.id, "tg_id": user.tg_id, "season_id": season_id,
                "source": user.source, "full_name": lead.full_name, "university": lead.university,
                "direction": lead.direction, "course": lead.course, "vacancy": lead.vacancy,
                "practice_type": lead.practice_type, "readiness": lead.readiness, "phone": lead.phone,
                "email": lead.email, "telegram": lead.telegram, "tags": tags, "quiz_score": lead.quiz_score,
                "best_profession": best, "season_place": place, "created_at": iso(lead.created_at)})
        log.info("lead created", extra={"fields": {"lead_id": lead.id, "user_id": user.id, "tags": tags}})
        return {"ok": True, **lead_out(lead)}

    async def current(self, user: User) -> dict[str, Any]:
        async with self.sm() as session:
            lead = (await session.execute(select(Lead).where(Lead.user_id == user.id)
                                          .order_by(Lead.created_at.desc(), Lead.id.desc()).limit(1))).scalar_one_or_none()
        if lead is None:
            raise AppError("lead_not_found", "No lead yet")
        return lead_out(lead)

    async def revoke(self, user: User) -> None:
        """152-FZ revocation: consent_revoked_at = now on every lead of the player that is not revoked yet, one
        `lead_revoked` outbox row each, same transaction. Repeating it is a no-op (204 again); no lead at all -> 404."""
        now = datetime.now(timezone.utc)
        async with self.sm() as session, session.begin():
            rows = (await session.execute(
                update(Lead).where(Lead.user_id == user.id, Lead.consent_revoked_at.is_(None))
                .values(consent_revoked_at=now, updated_at=now).returning(Lead.id, Lead.season_id))).all()
            if not rows:
                if (await session.execute(select(Lead.id).where(Lead.user_id == user.id).limit(1))).first() is None:
                    raise AppError("lead_not_found", "No lead to revoke")
                return
            for r in rows:
                await enqueue(session, "lead_revoked", {"lead_id": r.id, "user_id": user.id, "tg_id": user.tg_id,
                                                        "season_id": r.season_id, "revoked_at": iso(now)})
        log.info("lead consent revoked", extra={"fields": {"user_id": user.id, "leads": len(rows)}})

