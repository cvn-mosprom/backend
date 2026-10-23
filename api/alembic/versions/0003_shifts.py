"""shifts: log of accepted submitShift commands (HR analytics)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "shifts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("building_id", sa.Text(), nullable=False),
        sa.Column("profession", sa.Text(), nullable=False),
        sa.Column("game_id", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("score", sa.REAL(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("difficulty", sa.REAL()),
        sa.Column("metrics", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("boost_mult", sa.REAL()),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_shifts"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_shifts_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_shifts_season_id_seasons"),
    )
    op.create_index("ix_shifts_user_created", "shifts", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_shifts_user_created", table_name="shifts")
    op.drop_table("shifts")
