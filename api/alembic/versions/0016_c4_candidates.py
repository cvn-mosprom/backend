"""C4: candidate pipeline - new lead statuses, assignee, stage timestamp, optimistic-lock version, notes

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-24

* statuses: new -> screening -> interview -> offer -> hired | rejected | reserve; the old `contacted` becomes `screening`;
* leads.assignee_id (hr_users), leads.stage_changed_at (backfilled: created_at for `new`, updated_at otherwise),
  leads.version (PATCH /api/hr/candidates/{id} compares it, bumped by every change incl. the bot's status buttons);
* lead_notes: HR notes of a card (text stays out of lead_events, which analysts may read);
* indexes for the list filters / sorts and the kanban columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)
NEW = "status in ('new','screening','interview','offer','hired','rejected','reserve')"
OLD = "status in ('new','contacted','interview','rejected','hired')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_leads_status"), "leads", type_="check")
    op.execute("UPDATE leads SET status = 'screening' WHERE status = 'contacted'")
    op.create_check_constraint(op.f("ck_leads_status"), "leads", NEW)

    op.add_column("leads", sa.Column("assignee_id", sa.BigInteger()))
    op.create_foreign_key("fk_leads_assignee_id_hr_users", "leads", "hr_users", ["assignee_id"], ["id"],
                          ondelete="SET NULL")
    op.add_column("leads", sa.Column("stage_changed_at", TSTZ, server_default=sa.func.now(), nullable=False))
    op.add_column("leads", sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False))
    op.execute("UPDATE leads SET stage_changed_at = CASE WHEN status = 'new' THEN created_at ELSE updated_at END")

    op.create_table(
        "lead_notes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lead_id", sa.BigInteger(), nullable=False),
        sa.Column("author_hr_id", sa.BigInteger()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_lead_notes"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], name="fk_lead_notes_lead_id_leads", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_hr_id"], ["hr_users.id"], name="fk_lead_notes_author_hr_id_hr_users",
                                ondelete="SET NULL"),
    )
    op.create_index("ix_lead_notes_lead_created", "lead_notes", ["lead_id", "created_at"])

    op.create_index("ix_leads_created_id", "leads", [sa.text("created_at DESC"), sa.text("id DESC")])
    op.create_index("ix_leads_status_stage", "leads", ["status", sa.text("stage_changed_at DESC"), sa.text("id DESC")])
    op.create_index("ix_leads_assignee", "leads", ["assignee_id"], postgresql_where=sa.text("assignee_id IS NOT NULL"))
    op.create_index("ix_leads_user_id", "leads", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_leads_user_id", table_name="leads")
    op.drop_index("ix_leads_assignee", table_name="leads")
    op.drop_index("ix_leads_status_stage", table_name="leads")
    op.drop_index("ix_leads_created_id", table_name="leads")
    op.drop_table("lead_notes")
    op.drop_column("leads", "version")
    op.drop_column("leads", "stage_changed_at")
    op.drop_constraint("fk_leads_assignee_id_hr_users", "leads", type_="foreignkey")
    op.drop_column("leads", "assignee_id")
    op.drop_constraint(op.f("ck_leads_status"), "leads", type_="check")
    # the old set has no screening / offer / reserve: contacted, interview, new keep the pipeline order closest to the new names
    op.execute("UPDATE leads SET status = CASE status WHEN 'screening' THEN 'contacted' WHEN 'offer' THEN 'interview' "
               "WHEN 'reserve' THEN 'new' ELSE status END")
    op.create_check_constraint(op.f("ck_leads_status"), "leads", OLD)
