"""account_claims.last_checked_at for the verify cooldown

Revision ID: 3c58aeb758b4
Revises: 0f28636c2759
Created: 2026-08-18 03:35:17.603632

Reviewed 2026-08-18. One nullable column, added and dropped. No backfill:
NULL is the correct value for an existing claim, and reads as "never checked",
which is exactly what an existing row means.

WHY THE COLUMN EXISTS: MAX_ATTEMPTS caps how MANY verification attempts a
seller gets; this caps how FAST. Each attempt is a billable Apify scrape, and a
seller who has just pasted the code into their bio in another tab will press
Verify every few seconds to see whether it has taken. See CHECK_COOLDOWN in
services/verification.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '3c58aeb758b4'
down_revision: str | None = '0f28636c2759'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, with no server_default: an existing claim genuinely has never
    # been checked on the clock, and NULL says that. Backfilling now() would
    # silently put every open claim into a cooldown it never earned.
    op.add_column(
        "account_claims",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Safe to drop: the column only paces attempts. Losing it re-opens the rate
    # gap it closes, but destroys nothing a seller can see.
    op.drop_column("account_claims", "last_checked_at")
