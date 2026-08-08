"""init prove the migration pipeline

Revision ID: f33154a36521
Revises: 
Created: 2026-08-08 09:29:42.030627

REVIEW BEFORE APPLYING. Autogenerate is a first draft, not an answer — it
misses renames (it sees a drop plus an add, which loses data), and it cannot
know about constraints you meant to add. Read every line.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'f33154a36521'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
