"""buyer_questions: hand a question we cannot answer to the shop

Revision ID: b3d7c8e15f42
Revises: e7f2a91c40db
Created: 2026-09-01 19:00:00.000000

WHY THE TABLE. A buyer asking "do you deliver to Kisumu?" is asking something
only the seller knows — we hold no delivery zones, no fees and no lead times.
Answering it ourselves would put a promise on the buyer's screen that the seller
never made, so the question goes to the shop instead.

WHY A ROW RATHER THAN A PASSING MESSAGE. The seller may be asleep, and Meta's
24-hour window may have closed on their thread, so the notification may never
arrive. A question that exists only as an outbound message is one that gets
lost. This way they can open `questions` tomorrow and it is still there.

THE PARTIAL INDEX IS THE WORKING QUERY. "What is still waiting on me" is the
only way this table is read in the seller's flow, and indexing just the open
rows keeps it small no matter how many answered ones accumulate.

THE STATE CHECK IS HAND-WRITTEN, as every state migration here has been —
`alembic revision --autogenerate` does not diff CHECK constraints on an existing
table, so a rail changed in the model and not here never reaches the database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7c8e15f42"
down_revision: str | None = "e7f2a91c40db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALL_AFTER = (
    "'new', 'browsing', 'listing', 'product', 'variant', 'cart', "
    "'checkout_name', 'checkout_delivery', 'address', 'paying', 'pricing', "
    "'naming', 'about', 'answering', 'pay_kind', 'pay_number'"
)
_ALL_BEFORE = (
    "'new', 'browsing', 'listing', 'product', 'variant', 'cart', "
    "'checkout_name', 'checkout_delivery', 'address', 'paying', 'pricing', "
    "'naming', 'about', 'pay_kind', 'pay_number'"
)

_SELLER_AFTER = (
    "'new', 'pricing', 'naming', 'about', 'answering', 'pay_kind', 'pay_number'"
)
_SELLER_BEFORE = "'new', 'pricing', 'naming', 'about', 'pay_kind', 'pay_number'"


def upgrade() -> None:
    op.create_table(
        "buyer_questions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("seller_id", sa.Integer(), nullable=False),
        sa.Column("buyer_phone", sa.String(length=20), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["seller_id"], ["sellers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(answer IS NULL) = (answered_at IS NULL)",
            name="ck_buyer_questions_answer_and_time_agree",
        ),
        sa.CheckConstraint(
            "answer IS NULL OR length(btrim(answer)) > 0",
            name="ck_buyer_questions_answer_not_blank",
        ),
    )
    op.create_index(
        "ix_buyer_questions_seller_id", "buyer_questions", ["seller_id"], unique=False
    )
    op.create_index(
        "ix_buyer_questions_open",
        "buyer_questions",
        ["seller_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("answered_at IS NULL"),
    )

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
    # A seller mid-answer loses a half-typed sentence, and nothing else: the
    # question row is what carries the state that matters, and it survives.
    op.execute("UPDATE wa_conversations SET state = 'new' WHERE state = 'answering'")

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

    op.drop_index("ix_buyer_questions_open", table_name="buyer_questions")
    op.drop_index("ix_buyer_questions_seller_id", table_name="buyer_questions")
    op.drop_table("buyer_questions")
