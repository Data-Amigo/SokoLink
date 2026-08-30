"""wa_conversations: a naming state for onboarding

Revision ID: f0e5f6c9ee7f
Revises: 3d04b2b8a8b9
Created: 2026-08-30 04:57:59.909638

REVIEW BEFORE APPLYING. Autogenerate is a first draft, not an answer — it
misses renames (it sees a drop plus an add, which loses data), and it cannot
know about constraints you meant to add. Read every line.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'f0e5f6c9ee7f'
down_revision: str | None = '3d04b2b8a8b9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Written by hand, as the last one was: `alembic revision --autogenerate`
    # does not diff CHECK constraints on an existing table, so a rail changed in
    # the model and not here never reaches the database.
    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        "state IN ('new', 'browsing', 'listing', 'product', 'cart', "
        "'address', 'paying', 'pricing', 'naming')",
    )

    # 'naming' joins 'new' and 'pricing' in not requiring a seller. Somebody
    # being asked what to call their shop does not have one yet — that is the
    # entire point of the state.
    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        "state IN ('new', 'pricing', 'naming') OR seller_id IS NOT NULL",
    )


def downgrade() -> None:
    op.execute("UPDATE wa_conversations SET state = 'new' WHERE state = 'naming'")

    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        "state IN ('new', 'pricing') OR seller_id IS NOT NULL",
    )

    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        "state IN ('new', 'browsing', 'listing', 'product', 'cart', "
        "'address', 'paying', 'pricing')",
    )
