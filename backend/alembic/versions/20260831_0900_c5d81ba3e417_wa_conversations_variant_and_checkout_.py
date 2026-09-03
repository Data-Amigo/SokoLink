"""wa_conversations: a variant step, and checkout asked one question at a time

Revision ID: c5d81ba3e417
Revises: a1c4e9b77d20
Created: 2026-08-31 09:00:00.000000

WRITTEN BY HAND, like every state migration before it. `alembic revision
--autogenerate` does not diff CHECK constraints on an existing table, so a rail
changed in the model and not here never reaches the database — and the failure
shows up as a route that works locally and 500s in production.

THREE STATES, TWO REASONS.

'variant' closes a hole rather than adding a feature: the product card printed
"Sizes: 37, 38, 39, 40" and then added to the basket without asking which, so a
seller received an order for sandals with no size. The columns to hold the
answer already existed on cart_items and order_items; only the question was
missing.

'checkout_name' and 'checkout_delivery' split one message that was doing the
work of a form — "reply with your name and where to deliver, separated by a
comma" — into the two questions a person would actually be asked, plus the
choice between delivery and collection that was never offered at all.

All three are BUYER states, so all three require a shop: the browsing_needs_
seller constraint is deliberately left alone.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c5d81ba3e417"
down_revision: str | None = "a1c4e9b77d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES_AFTER = (
    "'new', 'browsing', 'listing', 'product', 'variant', 'cart', "
    "'checkout_name', 'checkout_delivery', 'address', 'paying', 'pricing', "
    "'naming', 'pay_kind', 'pay_number'"
)
_STATES_BEFORE = (
    "'new', 'browsing', 'listing', 'product', 'cart', 'address', 'paying', "
    "'pricing', 'naming', 'pay_kind', 'pay_number'"
)


def upgrade() -> None:
    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        f"state IN ({_STATES_AFTER})",
    )


def downgrade() -> None:
    # Park anyone mid-flow somewhere the narrower CHECK accepts. 'cart' rather
    # than 'new', because these are all buyers who have a basket: sending them
    # back to the basket loses a question, while 'new' would lose the shop and
    # the constraint requiring one would then refuse the row.
    op.execute(
        "UPDATE wa_conversations SET state = 'cart' "
        "WHERE state IN ('variant', 'checkout_name', 'checkout_delivery')"
    )

    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        f"state IN ({_STATES_BEFORE})",
    )
