"""
Seller — a shop and its public identity.

    @handle ──> Seller ──> slug ──> sokolink.shop/<slug>
                  │
                  └──> Product[]

WHY the slug is separate from the handle: the handle is TikTok's, and a seller
can change it there without telling us. The slug is ours and is PERMANENT once
published, because it is the URL the seller puts in their bio and sends to
buyers. Deriving it fresh on every sync would silently break every link they
have already shared.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.scrape_job import ScrapeJob


class Seller(Base):
    """A Kenyan TikTok seller and the storefront that belongs to them."""

    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: TikTok handle WITHOUT the leading @, lowercased. Normalised on write so
    #: one seller cannot exist twice as "@Shop" and "shop".
    #: Nullable: a seller can start from manual uploads alone and connect TikTok
    #: later — or never.
    tiktok_handle: Mapped[str | None] = mapped_column(String(64), unique=True)

    #: Public URL segment. Permanent once published.
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: E.164 without the +, e.g. 254712345678. This is what wa.me links and
    #: M-Pesa both use, so a malformed value silently breaks the money path.
    #: Validated on write, never trusted from a form.
    whatsapp_number: Mapped[str | None] = mapped_column(String(20))

    bio: Mapped[str | None] = mapped_column(String(500))
    avatar_url: Mapped[str | None] = mapped_column(String(500))

    #: Captured at sync time. Used by Soko Intel to benchmark the seller against
    #: others in their niche.
    follower_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: False until the seller has reviewed their drafts and gone live.
    #: An unpublished storefront 404s rather than showing half-parsed junk.
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: When the profile was last pulled from TikTok. Drives the once-per-day
    #: scrape guard, and answers "why has nothing changed?" on the dashboard.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    products: Mapped[list[Product]] = relationship(
        back_populates="seller", cascade="all, delete-orphan"
    )
    scrape_jobs: Mapped[list[ScrapeJob]] = relationship(
        back_populates="seller", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # A published shop with no way to contact the seller is a dead end for
        # every buyer who reaches it — the entire funnel ends in silence.
        CheckConstraint(
            "is_published = false OR whatsapp_number IS NOT NULL",
            name="ck_sellers_published_needs_whatsapp",
        ),
        # Slugs appear in URLs and are read aloud; enforce the shape in the DB
        # so no code path can create one a buyer cannot type.
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="ck_sellers_slug_format",
        ),
        CheckConstraint("follower_count >= 0", name="ck_sellers_follower_count_non_negative"),
        # The storefront looks up by slug on every buyer page load.
        Index("ix_sellers_slug", "slug"),
    )

    def __repr__(self) -> str:
        return f"<Seller id={self.id} slug={self.slug!r}>"
