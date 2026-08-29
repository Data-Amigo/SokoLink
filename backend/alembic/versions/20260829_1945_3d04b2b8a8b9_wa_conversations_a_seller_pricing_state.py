"""wa_conversations: a seller pricing state

Revision ID: 3d04b2b8a8b9
Revises: 1e60b4caca1b
Created: 2026-08-29 19:45:29.811267

REVIEW BEFORE APPLYING. Autogenerate is a first draft, not an answer — it
misses renames (it sees a drop plus an add, which loses data), and it cannot
know about constraints you meant to add. Read every line.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '3d04b2b8a8b9'
down_revision: str | None = '1e60b4caca1b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # WRITTEN BY HAND. Autogenerate produced `pass`: it does not diff CHECK
    # constraints on an existing table, so a rail changed in the model and not
    # here would simply never reach the database — the code would allow a state
    # Postgres still rejects, and the failure would land on a seller mid-chat.
    op.drop_constraint(
        "ck_wa_conversations_state_valid", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        "state IN ('new', 'browsing', 'listing', 'product', 'cart', "
        "'address', 'paying', 'pricing')",
    )

    # 'pricing' is a SELLER state. A seller pricing their own drafts has never
    # chosen a shop to browse, so requiring seller_id here would force their
    # conversation to point at their own storefront as though they shopped
    # there.
    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        "state IN ('new', 'pricing') OR seller_id IS NOT NULL",
    )


def downgrade() -> None:
    # Any conversation sitting in 'pricing' would violate the narrower
    # constraint, so it is moved back to 'new' before the rail is restored.
    op.execute("UPDATE wa_conversations SET state = 'new' WHERE state = 'pricing'")

    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        "state = 'new' OR seller_id IS NOT NULL",
    )

    op.drop_constraint(
        "ck_wa_conversations_state_valid", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        "state IN ('new', 'browsing', 'listing', 'product', 'cart', "
        "'address', 'paying')",
    )
