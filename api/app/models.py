"""SQLAlchemy models. Alembic migrations in api/alembic are hand-written to match."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, ForeignKey, Index, Integer, MetaData, Text, text
from sqlalchemy.dialects.postgresql import JSONB
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
    created_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


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
    # last GameSnapshot returned by the sim; served by GET /api/factory/me in degraded mode (sim down)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = _tstz(server_default=sa.func.now(), nullable=False)


Index("ix_factories_season_stars", Factory.season_id, Factory.stars.desc())
Index("ix_factories_season_quality", Factory.season_id, Factory.quality_avg.desc())
