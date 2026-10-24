"""C1: achievements (earned, progress, player scores)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-24

`achievements_earned` is append-only (an earned achievement is never revoked): unique (user_id, achievement_id, season_key),
season_key 0 = not seasonal, else the season id. `achievement_progress` holds the progress bars and, under the internal id
`_counters`, the engine's monotonic counters (meta). `player_scores` is the denormalised row per (user, season) for sorting;
competency / fit are filled by C2.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "achievements_earned",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("achievement_id", sa.Text(), nullable=False),
        sa.Column("season_key", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("earned_season_id", sa.Integer()),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("earned_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("catalog_version", sa.Integer(), nullable=False),
        sa.Column("granted_via", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_achievements_earned"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_achievements_earned_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["earned_season_id"], ["seasons.id"], name="fk_achievements_earned_earned_season_id_seasons"),
        sa.UniqueConstraint("user_id", "achievement_id", "season_key", name="uq_achievements_earned_user_ach_season"),
    )
    op.create_index("ix_achievements_earned_user_earned", "achievements_earned", ["user_id", "earned_at"])
    op.create_index("ix_achievements_earned_ach", "achievements_earned", ["achievement_id"])

    op.create_table(
        "achievement_progress",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("achievement_id", sa.Text(), nullable=False),
        sa.Column("season_key", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("current", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("target", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("meta", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("updated_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "achievement_id", "season_key", name="pk_achievement_progress"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_achievement_progress_user_id_users", ondelete="CASCADE"),
    )

    op.create_table(
        "player_scores",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("achievement_points", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("season_points", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("competency", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("fit", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("evaluated_at", TSTZ),
        sa.Column("updated_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "season_id", name="pk_player_scores"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_player_scores_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_player_scores_season_id_seasons"),
    )
    op.create_index("ix_player_scores_season_points", "player_scores", ["season_id", sa.text("achievement_points DESC")])


def downgrade() -> None:
    op.drop_index("ix_player_scores_season_points", table_name="player_scores")
    op.drop_table("player_scores")
    op.drop_table("achievement_progress")
    op.drop_index("ix_achievements_earned_ach", table_name="achievements_earned")
    op.drop_index("ix_achievements_earned_user_earned", table_name="achievements_earned")
    op.drop_table("achievements_earned")
