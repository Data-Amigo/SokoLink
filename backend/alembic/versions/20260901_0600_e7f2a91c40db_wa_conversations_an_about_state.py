"""wa_conversations: a state for a seller describing their own shop

Revision ID: e7f2a91c40db
Revises: c5d81ba3e417
Created: 2026-09-01 06:00:00.000000

WRITTEN BY HAND, like every state migration before it. `alembic revision
--autogenerate` does not diff CHECK constraints on an existing table, so a rail
changed in the model and not here never reaches the database — and the failure
shows up as a route that works locally and 500s in production.

WHY THE STATE EXISTS. The buyer's welcome opens with a line saying what the shop
sells. It can derive one from the seller's categories, and that is serviceable,
but the seller's own words are better and this is the first thing every one of
their customers reads. `about` in the chat is how they write it.

IT IS A SELLER STATE, so it joins 'new', 'pricing', 'naming' and the payment
states in not requiring a shop to browse: the person is running their own.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e7f2a91c40db"
down_revision: str | None = "c5d81ba3e417"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_AFTER = (
    "'new', 'browsing', 'listing', 'product', 'variant', 'cart', "
    "'checkout_name', 'checkout_delivery', 'address', 'paying', 'pricing', "
    "'naming', 'about', 'pay_kind', 'pay_number'"
)
_ALL_BEFORE = (
    "'new', 'browsing', 'listing', 'product', 'variant', 'cart', "
    "'checkout_name', 'checkout_delivery', 'address', 'paying', 'pricing', "
    "'naming', 'pay_kind', 'pay_number'"
)

_SELLER_AFTER = "'new', 'pricing', 'naming', 'about', 'pay_kind', 'pay_number'"
_SELLER_BEFORE = "'new', 'pricing', 'naming', 'pay_kind', 'pay_number'"


def upgrade() -> None:
    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        f"state IN ({_ALL_AFTER})",
    )

    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        f"state IN ({_SELLER_AFTER}) OR seller_id IS NOT NULL",
    )


def downgrade() -> None:
    # Park anyone mid-sentence at 'new'. They lose a half-typed description,
    # which is not saved until they send it, and nothing else.
    op.execute("UPDATE wa_conversations SET state = 'new' WHERE state = 'about'")

    op.drop_constraint(
        "ck_wa_conversations_browsing_needs_seller", "wa_conversations", type_="check"
    )
    op.create_check_constraint(
        "ck_wa_conversations_browsing_needs_seller",
        "wa_conversations",
        f"state IN ({_SELLER_BEFORE}) OR seller_id IS NOT NULL",
    )

    op.drop_constraint("ck_wa_conversations_state_valid", "wa_conversations", type_="check")
    op.create_check_constraint(
        "ck_wa_conversations_state_valid",
        "wa_conversations",
        f"state IN ({_ALL_BEFORE})",
    )
