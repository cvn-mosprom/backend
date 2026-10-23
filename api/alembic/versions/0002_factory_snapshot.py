"""factories.snapshot: last sim snapshot for the degraded mode of GET /api/factory/me

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable without a default: a metadata-only change in Postgres, no table rewrite. Rows created before this
    # migration get their snapshot with the next write (bootstrap / factory/me / advance / command).
    op.add_column("factories", sa.Column("snapshot", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("factories", "snapshot")
