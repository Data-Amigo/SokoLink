"""
SocialAccount — one connected platform account belonging to a seller.

    Seller ──┬──> SocialAccount (tiktok/@zumamitumbabales)
             ├──> SocialAccount (instagram/@zuma_bales)
             └──> SocialAccount (facebook/ZumaBales)
                        │
                        └──> Product[]

WHY a table rather than columns on Seller: a column per platform
(`tiktok_handle`, `instagram_handle`, …) means a migration every time a
platform is added, and no place to record per-account state like "when did we
last sync THIS one". A row per connection makes adding Jumia a data change
rather than a schema change.

Generalised on 2026-08-08, before any platform beyond TikTok had an engine —
deliberately, while there were zero production rows to migrate.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import Platform

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.seller import Seller


class SocialAccount(Base):
    """A seller's account on one platform."""

    __tablename__ = "social_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    seller: Mapped[Seller] = relationship(back_populates="social_accounts")

    platform: Mapped[str] = mapped_column(String(20), nullable=False)

    #: Handle WITHOUT the leading @, lowercased. Normalised on write so one
    #: account cannot be connected twice as "@Shop" and "shop".
    handle: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Auto-filled from the platform on connect ─────────────────────────────
    # The seller confirms rather than types. Every field here came back
    # populated for a real account in spike 01.
    display_name: Mapped[str | None] = mapped_column(String(120))
    avatar_url: Mapped[str | None] = mapped_column(String(500))

    #: The profile bio. Kenyan sellers routinely put phone numbers here — one
    #: real bio held three — which is why onboarding can pre-fill the WhatsApp
    #: number instead of asking for it.
    bio: Mapped[str | None] = mapped_column(String(1000))

    follower_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: False when the seller disconnects. Kept rather than deleted so their
    #: imported products keep a coherent origin, and so reconnecting is not a
    #: fresh import of everything.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # ── Ownership proof ──────────────────────────────────────────────────────
    # Without this, a handle is just a string someone typed. A stranger could
    # claim another seller's account, have us scrape her videos and photos, and
    # publish a storefront pointing at THEIR WhatsApp number — sales diversion
    # that is invisible to the buyer.
    #
    # AN UNVERIFIED ACCOUNT CANNOT BE SYNCED. That is the rail, enforced in
    # services/verification.py and tested; it means an unproven claim can never
    # produce a single product.

    #: When ownership was proven. NULL means unproven, and unproven means unusable.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: bio_code | oauth. NULL until verified.
    verification_method: Mapped[str | None] = mapped_column(String(20))

    #: The one-time code the seller must place in their bio. Cleared the moment
    #: verification succeeds — a live code lying around is a standing target.
    verification_code: Mapped[str | None] = mapped_column(String(32))

    #: Codes expire so an abandoned attempt cannot be resumed months later by
    #: whoever controls the account then.
    verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Drives the once-per-day scrape guard, and answers "why has nothing
    #: changed?" on the dashboard instead of in a support message.
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    products: Mapped[list[Product]] = relationship(back_populates="social_account")

    __table_args__ = (
        CheckConstraint(
            "platform IN ('tiktok', 'instagram', 'facebook', 'jumia')",
            name="ck_social_accounts_platform_valid",
        ),
        # MANUAL is not connectable — it is the absence of a platform account.
        CheckConstraint("platform <> 'manual'", name="ck_social_accounts_not_manual"),
        CheckConstraint(
            "follower_count >= 0 AND post_count >= 0",
            name="ck_social_accounts_counts_non_negative",
        ),
        # A verified account must record HOW it was verified — otherwise we
        # cannot tell a bio-code proof from an OAuth one when auditing later,
        # or re-verify when a method is retired.
        CheckConstraint(
            "verified_at IS NULL OR verification_method IS NOT NULL",
            name="ck_social_accounts_verified_has_method",
        ),
        CheckConstraint(
            "verification_method IS NULL OR verification_method IN ('bio_code', 'oauth')",
            name="ck_social_accounts_verification_method_valid",
        ),
        # One account per platform per seller. Connecting a second TikTok would
        # make "sync my feed" ambiguous.
        UniqueConstraint("seller_id", "platform", name="uq_social_accounts_seller_platform"),
        # A handle can only be claimed once across the whole system, or two
        # sellers could both publish the same shop's products.
        UniqueConstraint("platform", "handle", name="uq_social_accounts_platform_handle"),
        Index("ix_social_accounts_seller", "seller_id"),
    )

    @property
    def platform_enum(self) -> Platform:
        """The platform as an enum, for display and dispatch."""
        return Platform(self.platform)

    @property
    def is_verified(self) -> bool:
        """Whether ownership has been proven. Unproven means unsyncable."""
        return self.verified_at is not None

    @property
    def can_sync(self) -> bool:
        """
        Whether we may pull this account's content.

        Both conditions matter: a disconnected account should not be scraped,
        and an unverified one must not be — the second is what stops a stranger
        harvesting someone else's catalogue.
        """
        return self.is_active and self.is_verified

    def __repr__(self) -> str:
        return f"<SocialAccount {self.platform}/@{self.handle}>"
