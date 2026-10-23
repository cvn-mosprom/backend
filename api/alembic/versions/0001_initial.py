"""users, seasons, factories

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text()),
        sa.Column("first_name", sa.Text()),
        sa.Column("last_name", sa.Text()),
        sa.Column("age_confirmed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("age_confirmed_at", TSTZ),
        sa.Column("university", sa.Text()),
        sa.Column("source", sa.Text()),
        sa.Column("ref_by", sa.BigInteger()),
        sa.Column("is_bot", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("tg_id", name="uq_users_tg_id"),
        sa.ForeignKeyConstraint(["ref_by"], ["users.id"], name="fk_users_ref_by_users", ondelete="SET NULL"),
    )

    op.create_table(
        "seasons",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("starts_at", TSTZ, nullable=False),
        sa.Column("ends_at", TSTZ, nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("closed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("closed_at", TSTZ),
        sa.PrimaryKeyConstraint("id", name="pk_seasons"),
    )

    op.create_table(
        "factories",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("credits", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("stars", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("level", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("xp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("bottles_season", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("quality_avg", sa.REAL(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_tick_at", TSTZ),
        sa.Column("updated_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_factories"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_factories_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_factories_season_id_seasons"),
    )
    # leaderboard tabs: general (stars) and quality, both per season
    op.create_index("ix_factories_season_stars", "factories", ["season_id", sa.text("stars DESC")])
    op.create_index("ix_factories_season_quality", "factories", ["season_id", sa.text("quality_avg DESC")])


def downgrade() -> None:
    op.drop_index("ix_factories_season_quality", table_name="factories")
    op.drop_index("ix_factories_season_stars", table_name="factories")
    op.drop_table("factories")
    op.drop_table("seasons")
    op.drop_table("users")
