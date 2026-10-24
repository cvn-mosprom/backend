"""C2: seasonal points index for the `achievements` leaderboard (scope=season)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-24

`player_scores.competency` / `fit` already exist (0012); C2 only fills them. The all-time board is served by
ix_player_scores_season_points (season_id, achievement_points DESC); this adds the matching index for season_points.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_player_scores_season_sp", "player_scores", ["season_id", sa.text("season_points DESC")])


def downgrade() -> None:
    op.drop_index("ix_player_scores_season_sp", table_name="player_scores")
