"""Public landing leads and anonymous, allowlisted funnel events.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column('leads', 'user_id', existing_type=sa.BigInteger(), nullable=True)
    op.add_column('leads', sa.Column('source', sa.Text()))
    op.add_column('leads', sa.Column('visitor_id', sa.Text()))
    op.add_column('leads', sa.Column('submission_id', sa.Text()))
    op.add_column('leads', sa.Column('submission_hash', sa.Text()))
    op.add_column('leads', sa.Column('receipt_token_hash', sa.Text()))
    op.create_unique_constraint('uq_leads_submission_id', 'leads', ['submission_id'])
    op.create_table('landing_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('session_id', sa.Text(), nullable=False),
        sa.Column('visitor_id', sa.Text()),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('props', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index('ix_landing_events_created', 'landing_events', ['created_at'])


def downgrade():
    # Public leads have no Telegram owner; never silently delete their candidate data on downgrade.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM leads WHERE user_id IS NULL) THEN RAISE EXCEPTION 'Public leads must be archived before downgrade'; END IF; END $$")
    op.drop_table('landing_events')
    op.drop_constraint('uq_leads_submission_id', 'leads', type_='unique')
    for col in ('receipt_token_hash', 'submission_hash', 'submission_id', 'visitor_id', 'source'):
        op.drop_column('leads', col)
    op.alter_column('leads', 'user_id', existing_type=sa.BigInteger(), nullable=False)
