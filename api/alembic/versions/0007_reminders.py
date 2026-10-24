"""S6 (player side): bot start + reminder consent on users, reminders_sent dedupe table, age_confirmed backfill

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("users", sa.Column("bot_started_at", TSTZ))
    op.add_column("users", sa.Column("notify_opt_in", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("users", sa.Column("notify_opt_in_at", TSTZ))
    op.add_column("users", sa.Column("notify_opt_out_at", TSTZ))

    # which (order, kind) reminder was already sent to which player: one reminder per kind per order, ever
    op.create_table(
        "reminders_sent",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sent_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "order_id", "kind", name="pk_reminders_sent"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_reminders_sent_user_id_users", ondelete="CASCADE"),
    )

    # S2 gap: ageConfirm only lived in the sim state. Players who confirmed before the API started mirroring it
    # into users.age_confirmed get the flag from their stored snapshot (the time of the confirmation is unknown).
    op.execute(
        "UPDATE users u SET age_confirmed = true, age_confirmed_at = COALESCE(u.age_confirmed_at, now()) "
        "FROM factories f WHERE f.user_id = u.id AND NOT u.age_confirmed "
        "AND f.snapshot #>> '{player,ageConfirmed}' = 'true'"
    )


def downgrade() -> None:
    op.drop_table("reminders_sent")
    op.drop_column("users", "notify_opt_out_at")
    op.drop_column("users", "notify_opt_in_at")
    op.drop_column("users", "notify_opt_in")
    op.drop_column("users", "bot_started_at")
