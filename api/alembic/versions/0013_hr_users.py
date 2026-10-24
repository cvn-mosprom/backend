"""C3: HR roles (hr_users) and the candidate event log (lead_events)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-24

hr_users replaces HR_ADMIN_TG_IDS as the source of truth (the api seeds the env ids as `admin` on startup when absent).
lead_events.kind is free text on purpose (no check constraint): later sprints add kinds without a migration.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "hr_users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("tg_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger()),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.BigInteger()),
        sa.PrimaryKeyConstraint("id", name="pk_hr_users"),
        sa.UniqueConstraint("tg_id", name="uq_hr_users_tg_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_hr_users_user_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["hr_users.id"], name="fk_hr_users_created_by_hr_users",
                                ondelete="SET NULL"),
        sa.CheckConstraint("role in ('hr','hr_lead','analyst','admin')", name=op.f("ck_hr_users_role")),
    )

    op.create_table(
        "lead_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lead_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_hr_id", sa.BigInteger()),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_lead_events"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name="fk_lead_events_lead_id_leads", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_hr_id"], ["hr_users.id"], name="fk_lead_events_actor_hr_id_hr_users",
                                ondelete="SET NULL"),
    )
    op.create_index("ix_lead_events_lead_created", "lead_events", ["lead_id", "created_at"])


def downgrade() -> None:
    op.drop_table("lead_events")
    op.drop_table("hr_users")
