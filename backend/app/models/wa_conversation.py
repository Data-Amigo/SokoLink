"""
Where a buyer has got to in a WhatsApp conversation.

    phone ──▶ WaConversation(seller, state, context) ──▶ what the next reply means

WHY STATE HAS TO BE STORED AT ALL. WhatsApp gives us one message at a time with
no session. A buyer who replies "2" has told us nothing on its own — it means a
category, a product, or a cart line depending entirely on what we last asked.
The conversation is the only thing that makes a bare number interpretable.

THE PHONE IS THE KEY, and it is unique across the table rather than per seller.
One person has one conversation with the bot at a time; if they switch shops
mid-chat, the seller on this row changes and the basket changes with it. Two
concurrent baskets in one thread would be indistinguishable to the buyer, who
sees a single stream of messages.

WHY NOT PUT THIS ON THE CART. A conversation exists before any shop is chosen
and outlives an emptied basket — "menu" after checkout must still work. Carts
are per seller and get cleared on order; this is per person and persists.

CONTEXT IS A SMALL JSON BAG, holding only what the LAST question needs to be
answered: the numbered options we just offered, and which product is on screen.
It is deliberately not a general store — anything durable belongs in a real
column on a real table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ConversationState:
    """
    The steps a buyer moves through, as plain strings.

    NOT AN ENUM CLASS ON THE COLUMN, because these are checked by a database
    constraint listing them explicitly — the rail and the code should read the
    same way, and an enum whose values are also duplicated in a CHECK is two
    places to forget.
    """

    #: Nothing chosen yet: we do not know which shop they want.
    NEW = "new"
    #: Looking at the category menu.
    BROWSING = "browsing"
    #: Looking at a numbered list of products within a category.
    LISTING = "listing"
    #: One product on screen, deciding whether to add it.
    PRODUCT = "product"
    #: Basket shown, deciding whether to check out.
    CART = "cart"
    #: Asked for a delivery address.
    ADDRESS = "address"
    #: Order placed; waiting for them to send the M-Pesa code.
    PAYING = "paying"

    ALL = (NEW, BROWSING, LISTING, PRODUCT, CART, ADDRESS, PAYING)


class WaConversation(Base):
    """One buyer's place in the conversation."""

    __tablename__ = "wa_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Bare digits with country code, matching WaMessage.from_number — the
    #: channel prefix is stripped at the webhook so every table agrees.
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)

    #: Which shop they are talking to. NULL until they name one, because the
    #: bot number is shared and the first message is what routes them.
    seller_id: Mapped[int | None] = mapped_column(
        ForeignKey("sellers.id", ondelete="SET NULL"), index=True
    )

    state: Mapped[str] = mapped_column(String(20), nullable=False, default=ConversationState.NEW)

    #: What the last question needs in order to be answerable: the options we
    #: numbered, and the product currently on screen. Nothing durable.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: THE BASKET THIS THREAD OWNS. A web buyer's cart token lives in a cookie
    #: that the browser hands back on every request; a chat has no cookie, so
    #: the conversation is the only place the token can live. Without it,
    #: get_or_create_cart mints a fresh empty basket on every single message —
    #: which is exactly the bug this column was added to fix.
    cart_token: Mapped[str | None] = mapped_column(String(64))

    #: The order they are paying for, once one exists. Kept so a code sent
    #: minutes later still lands on the right order.
    order_reference: Mapped[str | None] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    seller = relationship("Seller")

    __table_args__ = (
        CheckConstraint(
            "state IN ('new', 'browsing', 'listing', 'product', 'cart', 'address', 'paying')",
            name="ck_wa_conversations_state_valid",
        ),
        # Past 'new' there must be a shop. Every later state's replies are about
        # a specific catalogue, so a conversation browsing nothing is incoherent.
        CheckConstraint(
            "state = 'new' OR seller_id IS NOT NULL",
            name="ck_wa_conversations_browsing_needs_seller",
        ),
        # A buyer waiting to pay must know which order they are paying for,
        # or an arriving M-Pesa code has nowhere to go.
        CheckConstraint(
            "state <> 'paying' OR order_reference IS NOT NULL",
            name="ck_wa_conversations_paying_needs_order",
        ),
        Index("ix_wa_conversations_updated", "updated_at"),
    )

    def __repr__(self) -> str:
        return f"<WaConversation {self.phone} state={self.state}>"
