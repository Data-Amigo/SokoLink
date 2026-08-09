"""
Seller — a shop and its public identity.

    Seller ──┬──> SocialAccount[]   the platforms they have connected
             ├──> Product[]         everything in their catalogue
             └──> slug              sokolink.shop/<slug>

WHY the slug is not derived from a platform handle: the handle belongs to
TikTok (or Instagram, or Facebook) and the seller can change it there without
telling us. The slug is OURS and is PERMANENT once published, because it is the
URL they put in their bio and send to buyers. Re-deriving it on every sync would
silently break every link they had already shared.

WHY there is no `tiktok_handle` column: a seller can connect several platforms,
and will. Connections live in ``social_accounts`` — see that module for the
reasoning.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import Platform

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.product import Product
    from app.models.scrape_job import ScrapeJob
    from app.models.social_account import SocialAccount


class Seller(Base):
    """A Kenyan seller and the storefront that belongs to them."""

    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The login this shop belongs to. One account, one shop.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), unique=True
    )
    account: Mapped[Account | None] = relationship(back_populates="seller")

    #: Public URL segment. Permanent once published.
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: E.164 without the +, e.g. 254712345678. This is what wa.me links and
    #: M-Pesa both use, so a malformed value silently breaks the money path.
    #: Validated on write, never trusted from a form.
    whatsapp_number: Mapped[str | None] = mapped_column(String(20))

    bio: Mapped[str | None] = mapped_column(String(1000))
    avatar_url: Mapped[str | None] = mapped_column(String(500))

    #: Seller-entered, because no platform exposes a structured location.
    #: Feeds delivery conversations and, later, local search.
    location: Mapped[str | None] = mapped_column(String(200))

    #: False until the seller has reviewed their drafts and gone live.
    #: An unpublished storefront 404s rather than showing half-parsed junk.
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    social_accounts: Mapped[list[SocialAccount]] = relationship(
        back_populates="seller", cascade="all, delete-orphan"
    )
    products: Mapped[list[Product]] = relationship(
        back_populates="seller", cascade="all, delete-orphan"
    )
    scrape_jobs: Mapped[list[ScrapeJob]] = relationship(
        back_populates="seller", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # A published shop with no way to contact the seller is a dead end for
        # every buyer who reaches it — the whole funnel ends in silence.
        CheckConstraint(
            "is_published = false OR whatsapp_number IS NOT NULL",
            name="ck_sellers_published_needs_whatsapp",
        ),
        # Slugs appear in URLs and get read aloud; enforce the shape in the DB
        # so no code path can create one a buyer cannot type.
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="ck_sellers_slug_format",
        ),
        # The storefront looks up by slug on every buyer page load.
        Index("ix_sellers_slug", "slug"),
    )

    def account_for(self, platform: Platform | str) -> SocialAccount | None:
        """
        The seller's connected account on one platform, if any.

        Args:
            platform: Which platform to look for.

        Returns:
            The account, or None when that platform is not connected.
        """
        wanted = str(platform)
        return next(
            (a for a in self.social_accounts if a.platform == wanted and a.is_active),
            None,
        )

    @property
    def connected_platforms(self) -> list[str]:
        """Platforms this seller has actively connected — for the dashboard."""
        return [a.platform for a in self.social_accounts if a.is_active]

    @property
    def has_any_connection(self) -> bool:
        """
        Whether any platform is connected.

        A seller with none is legitimate: manual uploads alone are a valid way
        to run a shop, and some sellers will never connect anything.
        """
        return bool(self.connected_platforms)

    def __repr__(self) -> str:
        return f"<Seller id={self.id} slug={self.slug!r}>"
