"""
Account — a login.

    Account (email + password hash) ──1:1──> Seller (the shop)

WHY separate from Seller: authentication and shop identity change at different
rates and for different reasons. An email address is private and editable; a
storefront slug is public and permanent. Keeping them apart also means a
response that serialises a Seller can never accidentally leak a password hash,
because the hash is not on that object at all.

EMAIL + PASSWORD FIRST, PHONE LATER (decided 2026-08-08). Phone + OTP is the
better fit for Kenyan sellers — the number is already their identity, and it is
what M-Pesa charges — but it needs an SMS provider we do not yet have. The
`phone` column is here from the start so that migration is additive.

Passwords are hashed with Argon2id, never stored or logged in any other form.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.seller import Seller


class Account(Base):
    """A seller's login credentials."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: Lowercased on write. Unique, so signup can tell a duplicate from a typo —
    #: but the SIGNUP RESPONSE must never reveal which, or it becomes an
    #: account-enumeration oracle.
    #:
    #: NULLABLE since 2026-08-23. A seller who signs up through WhatsApp proves
    #: a phone number and has no reason to give an email — demanding one is the
    #: friction that flow exists to remove. Either `email` or `phone` identifies
    #: an account; the constraint below requires at least one.
    email: Mapped[str | None] = mapped_column(String(255), unique=True)

    #: Argon2id. Never serialised, never logged, never compared outside
    #: `app.security`.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    #: E.164 without the +. Optional today; the anchor for phone + OTP login
    #: when an SMS provider exists.
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)

    full_name: Mapped[str | None] = mapped_column(String(120))

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    #: The shops this account owns. ONE-TO-MANY since 2026-09: multi-shop lets a
    #: seller run several shops on one WhatsApp number. Was a one-to-one
    #: ``seller``; the ``seller`` property below preserves that single-shop view
    #: for the web workspace and older callers, which still manage one at a time.
    sellers: Mapped[list[Seller]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        order_by="Seller.id",
    )

    @property
    def seller(self) -> Seller | None:
        """
        The account's primary shop — its first shop that is not archived.

        A COMPATIBILITY SHIM. Before multi-shop, one account had exactly one
        ``seller`` and the whole web workspace reads ``account.seller``. Rather
        than rewrite all of that at once, this keeps a single-shop view: the
        chat chooses an ACTIVE shop per conversation (``managing_shop_id``),
        while the web keeps operating on this primary one until it grows its own
        shop switcher. Returns None only for an account with no shop at all.
        """
        live = [s for s in self.sellers if s.archived_at is None]
        candidates = live or self.sellers
        return candidates[0] if candidates else None

    __table_args__ = (
        # Lowercase enforced in the database, so "Guru@x.com" and "guru@x.com"
        # can never become two accounts no matter which code path inserts them.
        CheckConstraint(
            "email IS NULL OR email = lower(email)", name="ck_accounts_email_lowercase"
        ),
        CheckConstraint(
            "email IS NULL OR position('@' in email) > 1", name="ck_accounts_email_shape"
        ),
        # An account with neither is unreachable and unloggable-into — it could
        # only ever be created by a bug, and it would be invisible to every
        # lookup path we have.
        CheckConstraint(
            "email IS NOT NULL OR phone IS NOT NULL",
            name="ck_accounts_has_an_identity",
        ),
        # Kenyan E.164 without the +, matching what M-Pesa and wa.me expect.
        CheckConstraint(
            "phone IS NULL OR phone ~ '^254[17][0-9]{8}$'",
            name="ck_accounts_phone_format",
        ),
        Index("ix_accounts_email", "email"),
    )

    def __repr__(self) -> str:
        # Deliberately no email: repr() lands in logs and tracebacks.
        return f"<Account id={self.id}>"
