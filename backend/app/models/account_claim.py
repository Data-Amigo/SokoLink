"""
AccountClaim — an UNPROVEN attempt to connect a social account.

    seller says "@zumamitumbabales is mine"
            │
            ▼
      AccountClaim  ──── code in bio? ──── no ───▶ stays a claim, or expires
       (proves nothing)      │
                            yes
                             ▼
                      SocialAccount  ← only ever exists when verified

WHY A SEPARATE TABLE. The bio-code flow needs somewhere to keep a code while the
seller goes off to edit their profile. Putting that on ``social_accounts`` would
mean unverified rows living in the connected-accounts table, and every read
would have to remember to filter them out. One forgotten filter and an unproven
claim is shown to a seller as "connected".

Splitting it makes the rule structural instead of remembered:

    **A row in social_accounts IS a verified account. There is no other kind.**

``SocialAccount.verified_at`` is NOT NULL, so the database itself refuses to
hold an unproven connection. A claim is the waiting room; it grants nothing.

Claims are transient. They expire, and they are deleted the moment they succeed.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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

if TYPE_CHECKING:
    from app.models.seller import Seller

#: Each verification attempt costs a paid scrape. Without a cap, a seller
#: hammering "Verify" — or a script doing it for them — burns Apify credit for
#: nothing.
MAX_ATTEMPTS = 10


class AccountClaim(Base):
    """A pending, unproven claim on a social account."""

    __tablename__ = "account_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    seller: Mapped[Seller] = relationship(back_populates="account_claims")

    platform: Mapped[str] = mapped_column(String(20), nullable=False)

    #: Handle WITHOUT the leading @, lowercased. Normalised on write.
    handle: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The one-time code the seller must place in their bio.
    code: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Claims expire so an abandoned attempt cannot be resumed months later by
    #: whoever controls that account by then.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Verification attempts so far. Each one is a paid scrape.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: When the last attempt was made, so attempts can be spaced out.
    #:
    #: MAX_ATTEMPTS caps the total; this caps the RATE. A seller editing their
    #: bio in another tab will press Verify every few seconds to see whether it
    #: has taken — that is normal, human behaviour, and without this it is ten
    #: billable scrapes in half a minute.
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "platform IN ('tiktok', 'instagram', 'facebook', 'jumia')",
            name="ck_account_claims_platform_valid",
        ),
        CheckConstraint("attempts >= 0", name="ck_account_claims_attempts_non_negative"),
        # One claim per platform per seller in flight. A second would make
        # "which handle am I proving?" ambiguous.
        UniqueConstraint("seller_id", "platform", name="uq_account_claims_seller_platform"),
        Index("ix_account_claims_seller", "seller_id"),
        # Sweeping expired claims scans by expiry.
        Index("ix_account_claims_expires", "expires_at"),
    )

    @property
    def is_expired(self) -> bool:
        """Whether this claim can still be completed."""
        from datetime import UTC

        expires = self.expires_at
        # Rows can come back naive depending on driver and column history;
        # treat those as UTC rather than crashing on the comparison.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires < datetime.now(UTC)

    @property
    def attempts_exhausted(self) -> bool:
        """Whether this claim has burned its allowance of paid checks."""
        return self.attempts >= MAX_ATTEMPTS

    def __repr__(self) -> str:
        return f"<AccountClaim {self.platform}/@{self.handle} (unproven)>"
