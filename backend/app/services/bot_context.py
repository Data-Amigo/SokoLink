"""
Telling the model who it is talking to, and what it can see.

    conversation + seller ──▶ a short briefing ──▶ agent/understand.py

WHY THE BRIEFING IS SHORT AND FACTUAL. Everything here is read from the database
immediately before the call, so it is true at the moment of asking. Nothing is
summarised, invented or carried over from an earlier turn — the model gets facts
and a question, and its job is to interpret one sentence against them.

WHY IT MATTERS MORE THAN THE PROMPT DOES. The same eight words mean opposite
things depending on who sent them:

    "Biggie Books"   from somebody being asked to name their shop  -> the name
    "Biggie Books"   from a buyer browsing a shop                  -> a search

A model with a beautiful persona and no context cannot tell those apart. A model
with a plain persona and this briefing can, every time.

WHAT IS DELIBERATELY LEFT OUT. No prices, no stock counts per item, no order
details, no buyer phone numbers. The model never needs them — code answers every
question that touches money — and anything included here is something it could
repeat back. The category names and the item count are the most it gets, because
those shape a search and cannot mislead anybody.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ConversationState, Product, ProductStatus, Seller, WaConversation

#: How many category names to name. Past this the briefing is a catalogue dump
#: and the useful signal — roughly what this shop sells — is already given.
MAX_CATEGORIES = 12

#: What the conversation is waiting to hear, in words the model can use. Only
#: the states where a person's free text is genuinely ambiguous appear; the rest
#: never reach the model, because code matches them first.
_WAITING_FOR: dict[str, str] = {
    ConversationState.NAMING: (
        "You just asked them what their shop should be called. Their reply is "
        "the name, usually wrapped in a sentence."
    ),
    ConversationState.ABOUT: (
        "You just asked them to describe what their shop sells. Their reply is that description."
    ),
    ConversationState.PRICING: (
        "You just asked the price of one of their items. Their reply should be a number."
    ),
    ConversationState.PAY_NUMBER: (
        "You just asked for the M-Pesa number buyers pay to. Their reply should be a number."
    ),
    ConversationState.CHECKOUT_NAME: (
        "You just asked whose name the order is in. Their reply is a person's name."
    ),
    ConversationState.ADDRESS: (
        "You just asked where to deliver. Their reply is a place in Kenya."
    ),
    ConversationState.PAYING: ("You are waiting for an M-Pesa confirmation code from them."),
    ConversationState.VARIANT: ("You just asked which size they want."),
}


def describe(
    db: Session,
    convo: WaConversation,
    *,
    owner: Seller | None,
    shopping_at: Seller | None,
) -> str:
    """
    A briefing on this conversation, for the model reading the next message.

    Args:
        db: Session.
        convo: Where the conversation had got to.
        owner: The shop this NUMBER runs, if any. Makes them a seller.
        shopping_at: The shop this conversation is browsing, if any. Makes them
            a buyer, and takes precedence — a seller who opened somebody else's
            link is shopping.

    Returns:
        Plain prose. Not JSON: the model reads this, it does not parse it.
    """
    lines: list[str] = []

    if shopping_at is not None:
        lines.append(f"They are a CUSTOMER, shopping at a shop called {shopping_at.display_name}.")
        lines.append(_stock_line(db, shopping_at))
        if owner is not None and owner.id != shopping_at.id:
            lines.append(
                f"They also run their own shop, {owner.display_name}, but right "
                f"now they are buying, not selling."
            )
    elif owner is not None:
        lines.append(
            f"They are a SELLER. They run a shop called {owner.display_name}, "
            f"which is currently {'open' if owner.is_published else 'closed'}."
        )
        lines.append(_stock_line(db, owner))
    else:
        lines.append(
            "They are NEW. We do not know yet whether they want to sell or to "
            "buy, and nothing has been set up for them."
        )

    waiting = _WAITING_FOR.get(convo.state)
    if waiting:
        lines.append(waiting)
    else:
        lines.append("You are not waiting on any particular answer from them.")

    return "\n".join(lines)


def _stock_line(db: Session, seller: Seller) -> str:
    """What the shop holds, in the two facts that shape a search."""
    count = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.PUBLISHED.value,
            )
        )
        or 0
    )

    categories = [
        name
        for name in db.scalars(
            select(Product.category)
            .where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.PUBLISHED.value,
                Product.category.is_not(None),
            )
            .distinct()
            .limit(MAX_CATEGORIES)
        ).all()
        if name
    ]

    if not count:
        return "That shop has nothing in it yet."

    if categories:
        return f"That shop has {count} items for sale, in: {', '.join(sorted(categories))}."
    return f"That shop has {count} items for sale."
