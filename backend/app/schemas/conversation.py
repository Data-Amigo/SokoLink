"""
What the model is allowed to conclude about a message it was handed.

    inbound text + shop context ──▶ Gemini (schema = Understanding) ──▶ code acts

WHY A CLOSED SET OF INTENTS. The model interprets; it never decides. Handing it
an enum means the worst it can do is choose the wrong one of fifteen known
actions — every one of which the code already implements, guards and tests. A
free-text "action" field would let it invent a capability we do not have, and
the first a seller would know is a promise the system cannot keep.

WHY ENTITIES ARE SEPARATE FROM THE REPLY. The model may extract a shop name, a
search term or a budget, because reading those out of a sentence is exactly what
it is good at and what a keyword matcher cannot do:

    "My shop is called Biggie Books"      -> SHOP_NAME, name="Biggie Books"
    "anything under 1000 bob"             -> BUDGET, max_price_kes=1000
    "do you have a revision book"         -> FIND_PRODUCT, query="revision book"

It never returns a price to charge, a stock level, or an order status. Those
come from the database, and the rule that the agent proposes while code disposes
is exactly the line between a shop assistant and a liability.

WHY THERE IS A REPLY FIELD AT ALL, given the above. Some messages have no action
behind them — a greeting, a thank you, a question about delivery. Answering
those with a menu is what made the thread feel like a machine. The reply is used
ONLY for those, and `Understanding.may_speak` is the single place that decides
which; anything touching money, stock or an order is written by code.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Intent(StrEnum):
    """
    Every conclusion the model may reach.

    Each maps to something the conversation already knows how to do. Adding one
    here without a handler would let the model route a person into silence.
    """

    # ── Anyone ──────────────────────────────────────────────────────────────
    GREET = "greet"
    """Saying hello, with nothing else asked."""

    HELP = "help"
    """Asking what this is or what they can do here."""

    SMALL_TALK = "small_talk"
    """Thanks, goodbyes, a question with no action behind it."""

    UNKNOWN = "unknown"
    """Genuinely unclear. The caller falls back rather than guessing."""

    ABOUT_THIS_ITEM = "about_this_item"
    """
    A question about the item on screen, answerable from what we hold — what it
    is made of, what sizes, what it costs. Code answers it from the product row.
    """

    FOR_THE_SELLER = "for_the_seller"
    """
    A question only the shop can answer: delivery to a place, what it costs,
    when it arrives, whether something can be held. ``question`` carries it.

    THERE IS NO "I DON'T KNOW" BRANCH. Guessing would put a promise on a
    buyer's screen that the seller never made, so it is handed to them instead.
    """

    ANSWER = "answer"
    """
    They are answering the question that was just put to them. Which field
    below holds it depends on what was asked — the briefing says which.
    """

    # ── Buying ──────────────────────────────────────────────────────────────
    BROWSE = "browse"
    """Wants to see what the shop has, without naming anything."""

    FIND_PRODUCT = "find_product"
    """Named or described an item. ``query`` carries what to search for."""

    BUDGET = "budget"
    """Named a ceiling. ``max_price_kes`` carries it."""

    ADD_TO_BASKET = "add_to_basket"
    """Wants the thing currently on screen."""

    VIEW_BASKET = "view_basket"
    CHECKOUT = "checkout"

    # ── Selling ─────────────────────────────────────────────────────────────
    SHOP_NAME = "shop_name"
    """Giving or correcting the shop's name. ``name`` carries it."""

    SET_ABOUT = "set_about"
    """Describing what the shop sells. ``about`` carries it."""

    SELLER_ORDERS = "seller_orders"
    SELLER_PAYMENTS = "seller_payments"
    SELLER_SHARE_LINK = "seller_share_link"
    SELLER_ADD_STOCK = "seller_add_stock"
    """Asking how to get their catalogue in."""

    SELLER_OPEN = "seller_open"
    """Wants the shop open for business."""


#: Below this the intent is treated as UNKNOWN and the caller falls back.
#:
#: A wrong confident answer costs more than a fallback here. Routing somebody
#: asking about delivery into checkout is worse than showing them a menu.
CONFIDENCE_THRESHOLD = 0.55

#: Intents whose reply is CONVERSATION rather than fact, and may therefore be
#: written by the model. Everything absent from this set is answered by code
#: reading the database, because a sentence about stock, price, an order or a
#: payment has to be true and the model has no way to know whether it is.
SPEAKABLE = frozenset({Intent.GREET, Intent.HELP, Intent.SMALL_TALK, Intent.UNKNOWN})


class Understanding(BaseModel):
    """What one inbound message meant."""

    intent: Intent = Field(description="The single closest action this message asks for.")

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How sure you are. Below 0.55 the message is treated as unclear.",
    )

    query: str | None = Field(
        default=None,
        description=(
            "For FIND_PRODUCT only: the words to search the catalogue with, "
            "stripped of politeness. 'do you have any revision books' -> "
            "'revision book'. Null otherwise."
        ),
    )

    max_price_kes: int | None = Field(
        default=None,
        description=(
            "For BUDGET only: the ceiling in whole Kenyan shillings. "
            "'under 1k' -> 1000. Null otherwise."
        ),
    )

    name: str | None = Field(
        default=None,
        description=(
            "For SHOP_NAME only: the shop's name ALONE, with the sentence "
            "around it removed. 'My shop is called Biggie Books' -> "
            "'Biggie Books'. Null otherwise."
        ),
    )

    about: str | None = Field(
        default=None,
        description=(
            "For SET_ABOUT only: what the shop sells, as the seller described it. Null otherwise."
        ),
    )

    question: str | None = Field(
        default=None,
        description=(
            "For FOR_THE_SELLER and ABOUT_THIS_ITEM: what they are asking, in "
            "their own words, tidied only enough to read on its own. Null "
            "otherwise."
        ),
    )

    # ── The answers a person gives, when ANSWER is the intent ───────────────
    # ONE FIELD PER QUESTION THE BOT ASKS. The briefing says which question is
    # outstanding, so exactly one of these should be filled and the rest left
    # null. They exist because people answer in sentences: "it goes for 1800",
    # "my name is Akinyi", "I'll take 40", "I've paid, code SLK7XA2B9C".

    price_kes: int | None = Field(
        default=None,
        description=(
            "When asked the price of an item: the number in whole shillings. "
            "'it goes for 1800' -> 1800. Never guess a price nobody stated."
        ),
    )

    person_name: str | None = Field(
        default=None,
        description=(
            "When asked whose name an order is in: the NAME ALONE. 'my name is "
            "Akinyi Otieno' -> 'Akinyi Otieno'."
        ),
    )

    size: str | None = Field(
        default=None,
        description=(
            "When asked which size: the size alone, exactly as the shop lists "
            "it. 'I will take 40' -> '40'."
        ),
    )

    mpesa_code: str | None = Field(
        default=None,
        description=(
            "When waiting on an M-Pesa confirmation: the code alone, uppercase. "
            "'I have paid, code SLK7XA2B9C' -> 'SLK7XA2B9C'."
        ),
    )

    phone_number: str | None = Field(
        default=None,
        description=(
            "When asked for an M-Pesa or till number: the digits alone. "
            "'my till is 123456' -> '123456'."
        ),
    )

    wants_delivery: bool | None = Field(
        default=None,
        description=(
            "When asked delivery or collection: true for delivery, false for "
            "collecting in person, null if they said neither."
        ),
    )

    reply: str | None = Field(
        default=None,
        description=(
            "A warm, brief answer IN THE SHOP'S VOICE, for greetings, thanks "
            "and questions with no action behind them. Two sentences at most. "
            "Never mention a price, an item, stock or an order — you cannot see "
            "those and code writes them. Null when an action answers better."
        ),
    )

    @property
    def is_confident(self) -> bool:
        """Whether this should be acted on at all."""
        return self.confidence >= CONFIDENCE_THRESHOLD

    @property
    def may_speak(self) -> bool:
        """
        Whether the model's own words may be shown to the person.

        THE LINE BETWEEN A SHOP ASSISTANT AND A LIABILITY. A model writing "yes,
        we have that in size 40 for 1,200" would be inventing stock and a price
        it has no way to know. Conversation is safe to generate; facts are not.
        """
        return self.intent in SPEAKABLE and bool(self.reply and self.reply.strip())
