"""server-side season rotation: frozen season results

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "season_results",
        sa.Column("season_id", sa.Integer(), sa.ForeignKey("seasons.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("place", sa.Integer(), nullable=False),  # general ranking (stars desc, user_id asc), bots included
        sa.Column("stars", sa.Integer(), nullable=False),
        sa.Column("quality", sa.REAL(), server_default=sa.text("0"), nullable=False),  # factories.quality_avg
        sa.Column("speed_bph", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("bottles", sa.BigInteger(), server_default=sa.text("0"), nullable=False),  # factories.bottles_season
        sa.Column("is_bot", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("season_id", "user_id", name="pk_season_results"),
    )
    op.create_index("ix_season_results_season_place", "season_results", ["season_id", "place"])
    op.create_index("ix_season_results_user", "season_results", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_season_results_user", table_name="season_results")
    op.drop_index("ix_season_results_season_place", table_name="season_results")
    op.drop_table("season_results")
