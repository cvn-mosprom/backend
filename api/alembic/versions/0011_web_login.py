"""S14: web login via the Telegram bot (login codes, deep-link login requests, web sessions)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-24

Codes, nonces and session tokens are stored only as sha256. All three tables reference users(id) ON DELETE CASCADE.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "web_login_codes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", TSTZ, nullable=False),
        sa.Column("used_at", TSTZ),
        sa.PrimaryKeyConstraint("id", name="pk_web_login_codes"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_web_login_codes_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("uq_web_login_codes_live_hash", "web_login_codes", ["code_hash"], unique=True,
                    postgresql_where=sa.text("used_at IS NULL"))
    op.create_index("ix_web_login_codes_user", "web_login_codes", ["user_id"])

    op.create_table(
        "web_login_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nonce_hash", sa.Text(), nullable=False),
        sa.Column("user_id", sa.BigInteger()),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", TSTZ, nullable=False),
        sa.Column("approved_at", TSTZ),
        sa.Column("consumed_at", TSTZ),
        sa.PrimaryKeyConstraint("id", name="pk_web_login_requests"),
        sa.UniqueConstraint("nonce_hash", name="uq_web_login_requests_nonce_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_web_login_requests_user_id_users",
                                ondelete="CASCADE"),
    )
    op.create_index("ix_web_login_requests_user", "web_login_requests", ["user_id"])

    op.create_table(
        "web_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", TSTZ, nullable=False),
        sa.Column("revoked_at", TSTZ),
        sa.Column("user_agent", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_web_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_web_sessions_token_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_web_sessions_user_id_users", ondelete="CASCADE"),
    )
    op.create_index("ix_web_sessions_user", "web_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("web_sessions")
    op.drop_table("web_login_requests")
    op.drop_table("web_login_codes")
