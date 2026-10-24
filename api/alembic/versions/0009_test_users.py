"""S13: users.is_test / factories.is_test (e2e and other test users are kept out of the top and the analytics)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-24

The flag is set by dev auth (app/testusers.py: name starting with "e2e", or tg_id >= 10**12). The backfill below applies
the same rule to rows that already exist (an e2e run before this migration). Real Telegram ids are far below 10**12.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("factories", sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.execute("UPDATE users SET is_test = true WHERE NOT is_bot AND (lower(first_name) LIKE 'e2e%' OR tg_id >= 1000000000000)")
    op.execute("UPDATE factories f SET is_test = true FROM users u WHERE u.id = f.user_id AND u.is_test")


def downgrade() -> None:
    op.drop_column("factories", "is_test")
    op.drop_column("users", "is_test")
