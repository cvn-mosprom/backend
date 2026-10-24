"""C5: vacancies of the HR CRM

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-24

`requirements` = {"competencies": {key: min 0..100}, "achievements": [catalog ids]}; the keys / ids are validated by the api (the
catalog lives in code and grows without a migration), so the column is plain jsonb. `profession_id` is the game profession
(content ProfessionId); no check constraint for the same reason. Deleting an HR user keeps the vacancy (`created_by` SET NULL).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TSTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "vacancies",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("profession_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("requirements", postgresql.JSONB(),
                  server_default=sa.text("""'{"competencies": {}, "achievements": []}'::jsonb"""), nullable=False),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TSTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_vacancies"),
        sa.ForeignKeyConstraint(["created_by"], ["hr_users.id"], name="fk_vacancies_created_by_hr_users",
                                ondelete="SET NULL"),
    )
    op.create_index("ix_vacancies_active_profession", "vacancies", ["active", "profession_id"])


def downgrade() -> None:
    op.drop_table("vacancies")
