"""S5: leads, outbox, quiz_answers, events

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("season_id", sa.Integer(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("birth_date", sa.Date()),
        sa.Column("phone", sa.Text()),
        sa.Column("email", sa.Text()),
        sa.Column("telegram", sa.Text()),
        sa.Column("university", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text()),
        sa.Column("course", sa.Text()),
        sa.Column("vacancy", sa.Text()),
        sa.Column("ready_from", sa.Date()),
        sa.Column("readiness", sa.Text()),
        sa.Column("practice_type", sa.Text()),
        sa.Column("consent", sa.Boolean(), nullable=False),
        sa.Column("consent_at", TSTZ, nullable=False),
        sa.Column("consent_text_version", sa.Text(), nullable=False),
        sa.Column("consent_revoked_at", TSTZ),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'new'"), nullable=False),
        sa.Column("hr_note", sa.Text()),
        sa.Column("best_profession", sa.Text()),
        sa.Column("season_place", sa.Integer()),
        sa.Column("quiz_score", sa.Integer()),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_leads"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_leads_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_leads_season_id_seasons"),
        sa.UniqueConstraint("user_id", "season_id", name="uq_leads_user_season"),
        sa.CheckConstraint("status in ('new','contacted','interview','rejected','hired')", name=op.f("ck_leads_status")),
        sa.CheckConstraint("practice_type is null or practice_type in ('paid','free','job')",
                           name=op.f("ck_leads_practice_type")),
    )
    op.create_index("ix_leads_status_created", "leads", ["status", "created_at"])
    op.create_index("ix_leads_university", "leads", ["university"])

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", TSTZ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
    )
    op.create_index("ix_outbox_pending", "outbox", ["id"], postgresql_where=sa.text("sent_at IS NULL"))

    op.create_table(
        "quiz_answers",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("question_id", sa.Text(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("answer", sa.Integer(), nullable=False),
        sa.Column("answered_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "question_id", name="pk_quiz_answers"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_quiz_answers_user_id_users", ondelete="CASCADE"),
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger()),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("props", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("ts", TSTZ, nullable=False),
        sa.Column("source", sa.Text()),
        sa.Column("received_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_events"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_events_user_id_users", ondelete="SET NULL"),
    )
    op.create_index("ix_events_name_ts", "events", ["name", "ts"])
    op.create_index("ix_events_user_ts", "events", ["user_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_events_user_ts", table_name="events")
    op.drop_index("ix_events_name_ts", table_name="events")
    op.drop_table("events")
    op.drop_table("quiz_answers")
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_leads_university", table_name="leads")
    op.drop_index("ix_leads_status_created", table_name="leads")
    op.drop_table("leads")
