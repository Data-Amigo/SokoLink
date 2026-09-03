"""wa_conversations: states for taking payment details in chat

Revision ID: a1c4e9b77d20
Revises: 07830c44ce7e
Created: 2026-08-30 14:00:00.000000

WRITTEN BY HAND, like the two state migrations before it. `alembic revision
--autogenerate` does not diff CHECK constraints on an existing table, so a rail
changed in the model and not here never reaches the database — and the failure
mode is a route that works locally and 500s in production.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "a1c4e9b77d20"
down_revision: str | None = "07830c44ce7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every state the conversation may hold after this migration.
_STATES_AFTER = (
    "'new', 'browsing', 'listing', 'product', 'cart', 'address', 'paying', "
    "'pricing', 'naming', 'pay_kind', 'pay_number'"
)
_STATES_BEFORE = (
    "'new', 'browsing', 'listing', 'product', 'cart', 'address', 'paying', "
    "'pricing', 'naming'"
)

#: States a SELLER can be in, which therefore have no shop to browse.
_SELLER_STATES_AFTER = "'new', 'pricing', 'naming', 'pay_kind', 'pay_number'"
_SELLER_STATES_BEFORE = "'new', 'pricing', 'naming'"


def upgrade() -> None:
    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        f"state IN ({_STATES_AFTER})",
    )

    # Both new states belong to a SELLER setting up their own shop, who has
    # never chosen a shop to browse. Requiring one would mean pointing their
    # conversation at their own storefront as though they were a customer.
    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        f"state IN ({_SELLER_STATES_AFTER}) OR seller_id IS NOT NULL",
    )


def downgrade() -> None:
    # Park anyone mid-setup at 'new' rather than letting the narrower CHECK
    # refuse to be created. They lose a half-finished answer, not a payment
    # method — nothing is saved until the number validates.
    op.execute(
        "UPDATE wa_conversations SET state = 'new' "
        "WHERE state IN ('pay_kind', 'pay_number')"
    )

    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        f"state IN ({_SELLER_STATES_BEFORE}) OR seller_id IS NOT NULL",
    )

    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        f"state IN ({_STATES_BEFORE})",
    )
