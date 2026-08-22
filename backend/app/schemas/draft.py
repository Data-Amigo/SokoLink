"""
The contract the vision model must satisfy.

    media + hints ──> Gemini (schema = ProductDraft) ──> validated object
                      constrained decoding

WHY a schema rather than "reply with JSON": asking nicely and hoping produces
fenced code blocks, chatty preambles and missing keys — reliably, in production.
Handing the API a schema CONSTRAINS generation so the output can only be a valid
instance. Parsing-and-praying is replaced by a guarantee.

WHAT IS DELIBERATELY ABSENT is as important as what is here. There is no stock
field and no contact field. The agent drafts words and reads a price it can see;
inventory and anything that could reach a buyer unattended are decided by code.

Every field earned its place in a spike against a real seller. `price_evidence`
in particular: it was added so a human could check the model's reading, and it
is what revealed that @zumamitumbabales prices in bales of thirty pairs rather
than per item — a fact no amount of design thinking had surfaced.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: Below this, a draft is held for review rather than trusted.
#:
#: The asymmetry is the whole point: a missing product costs the seller one
#: listing, a wrong price costs them an argument with a buyer holding a
#: screenshot. We bias hard toward review.
CONFIDENCE_THRESHOLD = 0.7

#: Any "price" above this is a misread, not a bargain — almost always a phone
#: number the model mistook for money. Postgres enforces the same ceiling.
MAX_PLAUSIBLE_PRICE_KES = 10_000_000


class ProductDraft(BaseModel):
    """One AI-drafted product, awaiting a human."""

    is_product: bool = Field(
        description=(
            "False when this is not a sellable item — a face, a shop interior, "
            "a text-only announcement, a greeting."
        )
    )

    name: str = Field(
        max_length=200,
        description="Short product name, e.g. 'Ladies Flat Sandals'. Not a caption.",
    )

    description: str = Field(
        default="",
        max_length=1000,
        description="One business-like sentence a buyer would find useful.",
    )

    price_kes: int | None = Field(
        default=None,
        description=(
            "Price in whole Kenyan shillings, ONLY if clearly stated — printed "
            "on the image, shown on screen, or spoken aloud. Null otherwise. "
            "Never estimate from appearance."
        ),
    )

    #: How many items one purchase contains.
    #:
    #: Found the hard way: "3000 for 30 pairs" is a bale, not a pair. Rendering
    #: a bulk price as a unit price is the most damaging error this system could
    #: make, because it is a lie about money that the buyer only discovers after
    #: paying.
    unit_quantity: int | None = Field(
        default=None,
        description=(
            "How many items the price buys. 1 for a single item. 30 for "
            "'3000 for 30 pairs'. Null if not stated."
        ),
    )

    unit_label: str | None = Field(
        default=None,
        max_length=32,
        description="What the units are called: 'pairs', 'pieces', 'bale', 'dozen'.",
    )

    #: The exact words the price was stated in.
    #:
    #: The audit trail behind an AI-read number: when a seller disputes a price,
    #: this is the evidence. Never shown to buyers.
    price_evidence: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "The exact words the price was stated in, e.g. '@3000 30pairs' or "
            "'mia tano'. Null if no price was found."
        ),
    )

    sizes: list[str] = Field(
        default_factory=list,
        description="Sizes stated, verbatim, e.g. ['37','38'] or ['S','M','L'].",
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="0-1, your confidence in the PRICE specifically.",
    )

    @property
    def has_price(self) -> bool:
        """Whether a usable price was found."""
        return self.price_kes is not None and self.price_kes > 0

    @property
    def is_confident(self) -> bool:
        """Whether this parse can skip escalation to a costlier tier."""
        return self.has_price and self.confidence >= CONFIDENCE_THRESHOLD

    @property
    def needs_review(self) -> bool:
        """Whether a human must look before this could ever go live."""
        return not self.is_confident or not self.is_product

    @property
    def is_plausible(self) -> bool:
        """
        Whether the price passes sanity checks before it reaches the database.

        Postgres enforces the same rules, but failing here produces a message
        naming the draft rather than an IntegrityError from three layers down.
        """
        if self.price_kes is None:
            return True
        return 0 < self.price_kes <= MAX_PLAUSIBLE_PRICE_KES
