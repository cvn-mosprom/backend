"""leaderboard: factories.speed_bph / university / is_bot + indexes

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("factories", sa.Column("speed_bph", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column("factories", sa.Column("university", sa.Text(), nullable=True))
    op.add_column("factories", sa.Column("is_bot", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    # backfill the copies for factories that already exist
    op.execute("UPDATE factories SET university = NULLIF(snapshot #>> '{player,university}', '') WHERE snapshot IS NOT NULL")
    op.execute("UPDATE factories f SET is_bot = true FROM users u WHERE u.id = f.user_id AND u.is_bot")
    op.create_index("ix_factories_season_speed", "factories", ["season_id", sa.text("speed_bph DESC")])
    op.create_index("ix_factories_season_university", "factories", ["season_id", "university"])


def downgrade() -> None:
    op.drop_index("ix_factories_season_university", table_name="factories")
    op.drop_index("ix_factories_season_speed", table_name="factories")
    op.drop_column("factories", "is_bot")
    op.drop_column("factories", "university")
    op.drop_column("factories", "speed_bph")
