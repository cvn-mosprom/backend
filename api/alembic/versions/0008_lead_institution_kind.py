"""S9: leads.institution_kind (vuz | college) from the client's lead form

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("leads", sa.Column("institution_kind", sa.Text()))
    op.create_check_constraint(op.f("ck_leads_institution_kind"), "leads",
                               "institution_kind is null or institution_kind in ('vuz','college')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_leads_institution_kind"), "leads", type_="check")
    op.drop_column("leads", "institution_kind")
