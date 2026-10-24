"""SQLAlchemy models. Alembic migrations in api/alembic are hand-written to match."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, ForeignKey, Index, Integer, MetaData, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _tstz(**kw: Any) -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), **kw)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(Text)
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    age_confirmed: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    age_confirmed_at: Mapped[datetime | None] = _tstz()
    university: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)  # Telegram start_param
    ref_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    is_bot: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    # S13: e2e / test player (app/testusers.py): never in the board, ranks, season results or analytics
    is_test: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    # S6 reminders (written by the bot): first /start, opt-in to order reminders, last opt-in / opt-out moments
    bot_started_at: Mapped[datetime | None] = _tstz()
    notify_opt_in: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    notify_opt_in_at: Mapped[datetime | None] = _tstz()
    notify_opt_out_at: Mapped[datetime | None] = _tstz()


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    starts_at: Mapped[datetime] = _tstz(nullable=False)
    ends_at: Mapped[datetime] = _tstz(nullable=False)
    days: Mapped[int] = mapped_column(nullable=False)
    closed: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    closed_at: Mapped[datetime | None] = _tstz()


class Factory(Base):
    """One factory per user. `state` is the opaque sim JSON; the other numeric columns are
    denormalised copies (leaderboard / bot analytics) refreshed whenever state is written."""

    __tablename__ = "factories"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    credits: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    stars: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    level: Mapped[int] = mapped_column(server_default=text("1"), nullable=False)
    xp: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    bottles_season: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    quality_avg: Mapped[float] = mapped_column(sa.REAL, server_default=text("0"), nullable=False)
    last_tick_at: Mapped[datetime | None] = _tstz()
    # leaderboard copies (S4): speed = bottles per hour (see game.measure_speed), university = snapshot.player.university,
    # is_bot = users.is_bot (kept here so board / rank queries need no join to filter bots)
    speed_bph: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    university: Mapped[str | None] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    is_test: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)  # = users.is_test (S13)
    # last GameSnapshot returned by the sim; served by GET /api/factory/me in degraded mode (sim down)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_factories_season_stars", Factory.season_id, Factory.stars.desc())
Index("ix_factories_season_quality", Factory.season_id, Factory.quality_avg.desc())
Index("ix_factories_season_speed", Factory.season_id, Factory.speed_bph.desc())
Index("ix_factories_season_university", Factory.season_id, Factory.university)


class Shift(Base):
    """Log of ACCEPTED submitShift commands, written in the transaction of the command (feeds HR analytics, S6).
    The daily limit is not derived from here: it lives in factories.state (shiftsLeft / shiftsDay, schema v2)."""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    building_id: Mapped[str] = mapped_column(Text, nullable=False)
    profession: Mapped[str] = mapped_column(Text, nullable=False)
    game_id: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)  # win | fail
    score: Mapped[float] = mapped_column(sa.REAL, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    difficulty: Mapped[float | None] = mapped_column(sa.REAL)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    boost_mult: Mapped[float | None] = mapped_column(sa.REAL)  # boost the shift gave the station (state.sim.layout)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_shifts_user_created", Shift.user_id, Shift.created_at)


class SeasonResult(Base):
    """Final standings of a closed season, frozen by app.seasons.rotate (general ranking incl. bots; is_bot lets analytics skip them)."""

    __tablename__ = "season_results"

    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    place: Mapped[int] = mapped_column(Integer, nullable=False)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    quality: Mapped[float] = mapped_column(sa.REAL, server_default=text("0"), nullable=False)
    speed_bph: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    bottles: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    is_bot: Mapped[bool] = mapped_column(server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_season_results_season_place", SeasonResult.season_id, SeasonResult.place)
Index("ix_season_results_user", SeasonResult.user_id)


# ---------------------------------------------------------------- S5: leads, outbox, quiz, events

class Lead(Base):
    """Candidate form of the case (one ACTIVE per user per season). Personal data: never logged. `consent_revoked_at` set
    = 152-FZ revocation: the row stays for the audit (status unchanged) but is excluded from the active funnel, mailings,
    GET /leads/me and the one-per-season uniqueness (partial unique index below), so the player may submit a new form."""

    __tablename__ = "leads"
    __table_args__ = (
        sa.Index("uq_leads_user_season_active", "user_id", "season_id", unique=True,
                 postgresql_where=sa.text("consent_revoked_at IS NULL")),
        sa.UniqueConstraint("submission_id", name="uq_leads_submission_id"),
        sa.CheckConstraint("status in ('new','screening','interview','offer','hired','rejected','reserve')", name="status"),
        sa.CheckConstraint("practice_type is null or practice_type in ('paid','free','job')", name="practice_type"),
        sa.CheckConstraint("institution_kind is null or institution_kind in ('vuz','college')", name="institution_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    visitor_id: Mapped[str | None] = mapped_column(Text)
    submission_id: Mapped[str | None] = mapped_column(Text)
    submission_hash: Mapped[str | None] = mapped_column(Text)
    receipt_token_hash: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    birth_date: Mapped[date | None] = mapped_column(sa.Date)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    telegram: Mapped[str | None] = mapped_column(Text)  # bare username, without '@'
    university: Mapped[str] = mapped_column(Text, nullable=False)
    institution_kind: Mapped[str | None] = mapped_column(Text)  # vuz | college (S9)
    direction: Mapped[str | None] = mapped_column(Text)  # the human label of the form's `department`, or free text
    course: Mapped[str | None] = mapped_column(Text)
    vacancy: Mapped[str | None] = mapped_column(Text)  # profession id
    ready_from: Mapped[date | None] = mapped_column(sa.Date)
    readiness: Mapped[str | None] = mapped_column(Text)  # now | season | later
    practice_type: Mapped[str | None] = mapped_column(Text)  # paid | free | job
    consent: Mapped[bool] = mapped_column(nullable=False)
    consent_at: Mapped[datetime] = _tstz(nullable=False)
    consent_text_version: Mapped[str] = mapped_column(Text, nullable=False)
    consent_revoked_at: Mapped[datetime | None] = _tstz()
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"), nullable=False)
    status: Mapped[str] = mapped_column(Text, server_default=text("'new'"), nullable=False)
    # C4: HR CRM pipeline - responsible HR, when the status last changed, optimistic-lock counter (every change bumps it)
    assignee_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("hr_users.id", ondelete="SET NULL"))
    stage_changed_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"), nullable=False)
    hr_note: Mapped[str | None] = mapped_column(Text)
    best_profession: Mapped[str | None] = mapped_column(Text)
    season_place: Mapped[int | None] = mapped_column(Integer)
    quiz_score: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_leads_status_created", Lead.status, Lead.created_at)
Index("ix_leads_university", Lead.university)
# C4: list / kanban of the HR CRM (migration 0016)
Index("ix_leads_created_id", Lead.created_at.desc(), Lead.id.desc())
Index("ix_leads_status_stage", Lead.status, Lead.stage_changed_at.desc(), Lead.id.desc())
Index("ix_leads_assignee", Lead.assignee_id, postgresql_where=text("assignee_id IS NOT NULL"))
Index("ix_leads_user_id", Lead.user_id)


class Outbox(Base):
    """Transactional outbox: the API inserts a row in the transaction of the business change, the bot delivers it
    (sent_at stays NULL until delivered; attempts / last_error are the bot's retry bookkeeping)."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # lead_created | lead_revoked | ...
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    sent_at: Mapped[datetime | None] = _tstz()
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)


Index("ix_outbox_pending", Outbox.id, postgresql_where=text("sent_at IS NULL"))


class QuizAnswer(Base):
    """One answer per (user, question); correctness is decided on the server (app/quiz_bank.py)."""

    __tablename__ = "quiz_answers"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    question_id: Mapped[str] = mapped_column(Text, primary_key=True)
    correct: Mapped[bool] = mapped_column(nullable=False)
    answer: Mapped[int] = mapped_column(Integer, nullable=False)  # chosen option index
    answered_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


class Event(Base):
    """Analytics event (funnel). `source` = users.source at ingestion time. Bots (users.is_bot) never write here."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    props: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    ts: Mapped[datetime] = _tstz(nullable=False)  # client time (checked: not from the future)
    source: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_events_name_ts", Event.name, Event.ts)
Index("ix_events_user_ts", Event.user_id, Event.ts)


# ---------------------------------------------------------------- S6: order reminders

class ReminderSent(Base):
    """Dedupe of the bot's order reminders: a (player, order, kind) pair is reminded at most once."""

    __tablename__ = "reminders_sent"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)  # eta | deadline
    sent_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


# ---------------------------------------------------------------- S14: web login via the Telegram bot

class WebLoginCode(Base):
    """One-time 6-digit code the bot hands to a player (`/login`). Only sha256(code) is stored. Single use: `used_at`
    is set when the code is exchanged for a session OR invalidated by a newer code / logout (partial unique index:
    two live codes never share a hash, so the code alone identifies the player)."""

    __tablename__ = "web_login_codes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    expires_at: Mapped[datetime] = _tstz(nullable=False)
    used_at: Mapped[datetime | None] = _tstz()


Index("uq_web_login_codes_live_hash", WebLoginCode.code_hash, unique=True, postgresql_where=text("used_at IS NULL"))
Index("ix_web_login_codes_user", WebLoginCode.user_id)


class WebLoginRequest(Base):
    """Deep-link login: the web page `start`s a request (random nonce, stored as sha256), the bot `approve`s it for a
    Telegram user, the page `poll`s until it can take the session token once (`consumed_at`)."""

    __tablename__ = "web_login_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    nonce_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    expires_at: Mapped[datetime] = _tstz(nullable=False)
    approved_at: Mapped[datetime | None] = _tstz()
    consumed_at: Mapped[datetime | None] = _tstz()


Index("ix_web_login_requests_user", WebLoginRequest.user_id)


class WebSession(Base):
    """Opaque bearer token of the web client (`Authorization: Bearer <token>`), stored as sha256. Sliding expiry."""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    expires_at: Mapped[datetime] = _tstz(nullable=False)
    revoked_at: Mapped[datetime | None] = _tstz()
    user_agent: Mapped[str | None] = mapped_column(Text)


Index("ix_web_sessions_user", WebSession.user_id)


# ---------------------------------------------------------------- C1: achievements

class AchievementEarned(Base):
    """One row per earned achievement, append-only (never revoked). `season_key` 0 = not seasonal, else the season id (so the
    unique key needs no NULLs). `earned_season_id` = the server season current when it was earned (informational, feeds
    `player_scores.season_points`); `granted_via` = hook | shift | recompute | backfill."""

    __tablename__ = "achievements_earned"
    __table_args__ = (sa.UniqueConstraint("user_id", "achievement_id", "season_key", name="uq_achievements_earned_user_ach_season"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    achievement_id: Mapped[str] = mapped_column(Text, nullable=False)
    season_key: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    earned_season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"))
    tier: Mapped[str] = mapped_column(Text, nullable=False)  # bronze | silver | gold
    points: Mapped[int] = mapped_column(Integer, nullable=False)
    earned_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    catalog_version: Mapped[int] = mapped_column(Integer, nullable=False)
    granted_via: Mapped[str] = mapped_column(Text, nullable=False)


Index("ix_achievements_earned_user_earned", AchievementEarned.user_id, AchievementEarned.earned_at)
Index("ix_achievements_earned_ach", AchievementEarned.achievement_id)


class AchievementProgress(Base):
    """Progress bar of one achievement (`current` <= `target`, monotonic). The internal id `_counters` (never in the
    catalog) keeps the engine's monotonic counters in `meta` (done orders seen, loans repaid)."""

    __tablename__ = "achievement_progress"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    achievement_id: Mapped[str] = mapped_column(Text, primary_key=True)
    season_key: Mapped[int] = mapped_column(Integer, server_default=text("0"), primary_key=True)
    current: Mapped[float] = mapped_column(sa.Float, server_default=text("0"), nullable=False)
    target: Mapped[float] = mapped_column(sa.Float, server_default=text("0"), nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    updated_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


class PlayerScore(Base):
    """Denormalised score per (player, season) for sorting (CRM, board tab). `achievement_points` = lifetime sum of the points
    of every earned achievement, `season_points` = the part earned during `season_id`. `competency` / `fit` are C2's.
    `evaluated_at` = start of the last FULL evaluation (the recompute job re-evaluates players changed after it)."""

    __tablename__ = "player_scores"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), primary_key=True)
    achievement_points: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    season_points: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    competency: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    fit: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    evaluated_at: Mapped[datetime | None] = _tstz()
    updated_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_player_scores_season_points", PlayerScore.season_id, PlayerScore.achievement_points.desc())
Index("ix_player_scores_season_sp", PlayerScore.season_id, PlayerScore.season_points.desc())  # C2: scope=season board


# ---------------------------------------------------------------- C3: HR roles and the candidate event log

HR_ROLES = ("hr", "hr_lead", "analyst", "admin")


class HrUser(Base):
    """Who may use the HR CRM (web, `/api/hr/*`) and the HR bot. Seeded from HR_ADMIN_TG_IDS as `admin` when absent
    (bootstrap only), then managed through `/api/hr/users`. `user_id` links the player row once the person has played / logged in."""

    __tablename__ = "hr_users"
    __table_args__ = (sa.CheckConstraint("role in ('hr','hr_lead','analyst','admin')", name="role"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    role: Mapped[str] = mapped_column(Text, nullable=False)  # hr | hr_lead | analyst | admin
    active: Mapped[bool] = mapped_column(server_default=text("true"), nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("hr_users.id", ondelete="SET NULL"))  # NULL = seed


class LeadEvent(Base):
    """History of a candidate: who did what (kind view|status|assign|note|message|export|tag; the set is validated in
    `app.hr.access.EVENT_KINDS`, not by a constraint, so later sprints add kinds without a migration). Every view of a card
    with personal data is one `view` row (the access log of 152-FZ)."""

    __tablename__ = "lead_events"
    __mapper_args__ = {"eager_defaults": True}  # created_at is readable right after the flush (async: no lazy loads)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    actor_hr_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("hr_users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_lead_events_lead_created", LeadEvent.lead_id, LeadEvent.created_at)


class LandingEvent(Base):
    """Anonymous landing funnel: only enumerated properties, no candidate/contact payloads."""
    __tablename__ = "landing_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    visitor_id: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    props: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_landing_events_created", LandingEvent.created_at)


# ---------------------------------------------------------------- C5: vacancies

class Vacancy(Base):
    """An open position of the HR CRM. `requirements` = {"competencies": {key: min 0..100}, "achievements": [catalog ids]} (validated
    by the api, `app.hr.vacancies`); candidates are matched against it by `vacancy_fit` (C2)."""

    __tablename__ = "vacancies"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    profession_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, server_default=text("''"), nullable=False)
    active: Mapped[bool] = mapped_column(server_default=text("true"), nullable=False)
    requirements: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("""'{"competencies": {}, "achievements": []}'::jsonb"""), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("hr_users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    updated_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_vacancies_active_profession", Vacancy.active, Vacancy.profession_id)


# ---------------------------------------------------------------- C4: candidate notes

class LeadNote(Base):
    """HR note on a candidate card. The text lives here, not in `lead_events` (analysts read the events, never the notes)."""

    __tablename__ = "lead_notes"
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lead_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    author_hr_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("hr_users.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_lead_notes_lead_created", LeadNote.lead_id, LeadNote.created_at)
