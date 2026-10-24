"""S14: a revoked lead no longer blocks a new one (one ACTIVE lead per user per season)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-24

152-FZ: the revoked row stays in `leads` (consent_revoked_at) for the audit, but GET /api/leads/me ignores it and the
player may submit a new form in the same season. So the plain unique (user_id, season_id) becomes a partial unique index
over the non-revoked rows. The downgrade restores the plain constraint: where a player has several leads in one season
(a revoked one and a new one) only the latest is kept, the older rows are deleted (the old schema cannot hold them).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_leads_user_season", "leads", type_="unique")
    op.create_index("uq_leads_user_season_active", "leads", ["user_id", "season_id"], unique=True,
                    postgresql_where=sa.text("consent_revoked_at IS NULL"))


def downgrade() -> None:
    op.execute("DELETE FROM leads a USING leads b WHERE a.user_id = b.user_id AND a.season_id = b.season_id "
               "AND (a.created_at, a.id) < (b.created_at, b.id)")
    op.drop_index("uq_leads_user_season_active", table_name="leads")
    op.create_unique_constraint("uq_leads_user_season", "leads", ["user_id", "season_id"])
