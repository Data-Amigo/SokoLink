"""
Product — one catalogue item, from any of the three ingestion paths.

    TikTok profile sync ─┐
    Pasted TikTok link  ─┼──> Product (DRAFT) ──> seller confirms ──> PUBLISHED
    Manual photo upload ─┘

WHY the constraints below are in the DATABASE rather than in service code: they
are the rails that make an AI-assisted catalogue safe. A model, a future agent,
a migration script and a hand-written query all have to obey Postgres. Only
application code has to remember to call the validator.

Three rails, each with a test that proves it refuses:

  1. published_requires_price  — the LLM can never push an unpriced item live
  2. stock_non_negative        — overselling is impossible at the storage layer
  3. unique tiktok_video_id    — re-scraping the same feed cannot duplicate items

MONEY IS INTEGER KES. `price_kes = 1500` is KSh 1,500. No floats near a price.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import PriceSource, ProductSource, ProductStatus

if TYPE_CHECKING:
    from app.models.seller import Seller


class Product(Base):
    """A single item on a storefront."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    seller: Mapped[Seller] = relationship(back_populates="products")

    # ── Provenance ───────────────────────────────────────────────────────────
    #: Which path created this. Decides what a re-sync may touch: a profile sync
    #: owns what it created, and must leave MANUAL products entirely alone.
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProductSource.TIKTOK_PROFILE.value
    )

    #: TikTok's video id. NULL for manual uploads.
    #: Unique so re-scraping a feed updates rather than duplicates — and
    #: Postgres permits many NULLs in a unique index, so manual products
    #: coexist without needing a partial index.
    tiktok_video_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    video_url: Mapped[str | None] = mapped_column(String(500))

    #: Cover image, stored BY US. TikTok CDN URLs expire, so a stored copy is
    #: the difference between a storefront that works next month and one full
    #: of broken images.
    cover_url: Mapped[str | None] = mapped_column(String(500))

    #: The original caption, kept verbatim. Ground truth when a parse is
    #: disputed, and the replay input when the prompt changes. Never overwritten.
    raw_caption: Mapped[str | None] = mapped_column(Text)

    hashtags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)

    # ── What the buyer sees ──────────────────────────────────────────────────
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: Price in whole KES. NULL while drafting — publishing requires it.
    price_kes: Mapped[int | None] = mapped_column(Integer)

    #: Sizes exactly as the seller wrote them: "30-34", "S/M/L", "one size".
    #: Deliberately not normalised — Kenyan sizing conventions vary by product,
    #: and inventing a canonical form loses information the buyer needs.
    sizes: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)

    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ProductStatus.DRAFT.value
    )

    # ── AI provenance ────────────────────────────────────────────────────────
    #: 0–1, the model's own confidence in its draft. Low-confidence items sort
    #: to the top of the review queue — that is where a wrong price hides.
    parse_confidence: Mapped[float | None] = mapped_column(Float)

    #: Which cascade tier produced the price. The feedback signal for the whole
    #: approach: if tier 3 rarely fires, the expensive path is not paying for
    #: itself. Without this column that is unanswerable.
    price_source: Mapped[str | None] = mapped_column(String(20))

    #: Set when a human confirms or corrects the draft. Distinct from status:
    #: an item can be reviewed and deliberately left unpublished.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Engagement at scrape time. Fuels Soko Intel outlier detection and costs
    #: nothing extra — it is already in every Apify payload.
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # ── RAIL 1: the AI can never push an unpriced item live ──────────────
        # The single most important constraint in the schema. Publishing is a
        # human act, and it requires a number.
        CheckConstraint(
            "status <> 'published' OR price_kes IS NOT NULL",
            name="ck_products_published_requires_price",
        ),
        # ── RAIL 2: overselling is impossible at the storage layer ───────────
        CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
        # ── RAIL 3: a price is a positive number or absent, never zero ───────
        # A KSh 0 product is always a parse failure, never a giveaway.
        CheckConstraint(
            "price_kes IS NULL OR price_kes > 0",
            name="ck_products_price_positive",
        ),
        # Guards the phone-number misparse: a model reading "0712345678" as a
        # price lands here rather than on the storefront.
        CheckConstraint(
            "price_kes IS NULL OR price_kes <= 10000000",
            name="ck_products_price_plausible",
        ),
        CheckConstraint(
            "parse_confidence IS NULL OR (parse_confidence >= 0 AND parse_confidence <= 1)",
            name="ck_products_confidence_range",
        ),
        CheckConstraint(
            "source IN ('tiktok_profile', 'tiktok_link', 'manual')",
            name="ck_products_source_valid",
        ),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_products_status_valid",
        ),
        CheckConstraint(
            "price_source IS NULL OR price_source IN ('caption', 'cover_image', 'video', 'seller')",
            name="ck_products_price_source_valid",
        ),
        # A manual product has no TikTok video behind it, by definition.
        CheckConstraint(
            "source <> 'manual' OR tiktok_video_id IS NULL",
            name="ck_products_manual_has_no_video_id",
        ),
        CheckConstraint(
            "views >= 0 AND likes >= 0 AND comments >= 0 AND shares >= 0",
            name="ck_products_metrics_non_negative",
        ),
        # The storefront query: this seller's published items.
        Index("ix_products_seller_status", "seller_id", "status"),
        # The dashboard review queue: this seller's drafts, worst parses first.
        Index("ix_products_seller_confidence", "seller_id", "parse_confidence"),
        # The re-sync guard: "does this seller already have this video?"
        Index("ix_products_seller_source", "seller_id", "source"),
    )

    @property
    def is_buyable(self) -> bool:
        """Whether a buyer can actually purchase this right now."""
        return (
            self.status == ProductStatus.PUBLISHED.value
            and self.stock > 0
            and self.price_kes is not None
        )

    @property
    def needs_review(self) -> bool:
        """
        Whether a human should look at this before it goes live.

        Anything the AI touched and nobody confirmed, or that has no price yet.
        """
        return self.reviewed_at is None or self.price_kes is None

    @property
    def price_is_ai_drafted(self) -> bool:
        """True when the price came from a model rather than a person."""
        return self.price_source is not None and self.price_source != PriceSource.SELLER.value

    def __repr__(self) -> str:
        return f"<Product id={self.id} title={self.title!r} status={self.status}>"
