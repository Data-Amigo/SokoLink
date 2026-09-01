"""
A customer asked the shop something the system does not know.

    buyer asks ──▶ BuyerQuestion (open) ──▶ seller's thread
                          │                        │
                          │                   they answer
                          ▼                        │
                   closed, answered ◀──────────────┘
                          │
                          └──▶ relayed to the buyer, in the shop's name

WHY THIS TABLE EXISTS AT ALL. "I don't know" is not an answer a shop gives. A
buyer asking "do you deliver to Kisumu?" or "how much is delivery?" is asking
something only the seller can answer — we hold no delivery zones, no fees and no
lead times, and inventing them would put a promise on the buyer's screen that
the seller never made and may not be able to keep.

So the question is HANDED OVER rather than guessed at. That is what a shop
assistant does when asked something above their pay grade, and it is the
difference between this feeling staffed and feeling like a form.

WHY IT IS A ROW AND NOT A MESSAGE IN PASSING. The seller may be asleep. Meta's
24-hour window may have closed on their thread, so the notification may never
arrive. A question that exists only as an outbound message is a question that
gets lost — this way the seller can open `questions` tomorrow and still see it,
and the buyer's question is never silently dropped on our side.

THE ANSWER IS THE SELLER'S OWN WORDS, relayed verbatim and attributed to their
shop. We do not summarise it, improve it, or write one on their behalf. The
moment we author an answer about delivery or availability, we have made a
commitment on somebody else's business.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.seller import Seller


class BuyerQuestion(Base):
    """One thing a customer asked that only the shop can answer."""

    __tablename__ = "buyer_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Which shop was asked. Answering is scoped to this — an open question is
    #: addressable, and without the check any seller could answer any other
    #: shop's customer in that shop's name.
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seller: Mapped[Seller] = relationship()

    #: Who asked, so the answer can be delivered. Bare digits with country code,
    #: matching WaConversation.phone — every table agrees on the format.
    buyer_phone: Mapped[str] = mapped_column(String(20), nullable=False)

    #: What they asked, verbatim. Kept as they typed it rather than as the model
    #: paraphrased it: the seller is answering a person, and a paraphrase is one
    #: more place for the meaning to shift before it reaches them.
    question: Mapped[str] = mapped_column(Text, nullable=False)

    #: The seller's reply, verbatim. NULL until they answer.
    answer: Mapped[str | None] = mapped_column(Text)

    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # An answered question has words in it. The pair moves together or the
        # row is lying about one of them, and "answered with nothing" would
        # relay an empty message to a buyer waiting on a real one.
        CheckConstraint(
            "(answer IS NULL) = (answered_at IS NULL)",
            name="ck_buyer_questions_answer_and_time_agree",
        ),
        CheckConstraint(
            "answer IS NULL OR length(btrim(answer)) > 0",
            name="ck_buyer_questions_answer_not_blank",
        ),
        # The seller's open list, oldest first — the person who has been waiting
        # longest is the one to answer next.
        Index(
            "ix_buyer_questions_open",
            "seller_id",
            "created_at",
            postgresql_where=answered_at.is_(None),
        ),
    )

    @property
    def is_open(self) -> bool:
        """Whether somebody is still waiting on this."""
        return self.answered_at is None

    def __repr__(self) -> str:
        state = "open" if self.is_open else "answered"
        return f"<BuyerQuestion {self.id} seller={self.seller_id} {state}>"
