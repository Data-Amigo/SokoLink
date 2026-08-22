"""jobs queue

Revision ID: 7bde9231c6a0
Revises: 8a73215c229c
Created: 2026-08-19 19:30:15.752437

Reviewed 2026-08-19. One new table and two indexes. Purely additive.

THE INDEX THAT MATTERS IS ``uq_jobs_dedupe_pending``, and it is PARTIAL — it
applies only to rows whose status is queued or running. That is what makes a
dedupe key reusable: a seller may sync @handle again tomorrow, but not twice
while the first sync is still pending. A plain unique index would let them sync
once, ever.

``ix_jobs_claimable`` serves the claim query, which runs once a second per
worker forever. Without it that is a sequential scan of a table that only grows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '7bde9231c6a0'
down_revision: str | None = '8a73215c229c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=50), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('seller_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('dedupe_key', sa.String(length=120), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('scheduled_for', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name='ck_jobs_status_valid'),
    sa.CheckConstraint('attempts >= 0', name='ck_jobs_attempts_non_negative'),
    sa.CheckConstraint('max_attempts >= 1', name='ck_jobs_max_attempts_at_least_one'),
    sa.ForeignKeyConstraint(['seller_id'], ['sellers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_jobs_claimable', 'jobs', ['status', 'scheduled_for'], unique=False)
    op.create_index('uq_jobs_dedupe_pending', 'jobs', ['dedupe_key'], unique=True, postgresql_where=sa.text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"))


def downgrade() -> None:
    op.drop_index('uq_jobs_dedupe_pending', table_name='jobs', postgresql_where=sa.text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"))
    op.drop_index('ix_jobs_claimable', table_name='jobs')
    op.drop_table('jobs')
