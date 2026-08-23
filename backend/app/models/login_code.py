"""
A one-time code sent to a seller's WhatsApp.

    phone ──▶ LoginCode(hashed, expires) ──▶ WhatsApp message
                        │
                  seller types it back ──▶ verified ──▶ session

WHY THE CODE IS HASHED. It is a credential — for the few minutes it lives, it
is the only thing standing between a stranger and somebody's shop. A database
dump, a log line or a backup containing usable login codes is the same class of
mistake as storing plaintext passwords, just with a shorter blast radius. It is
hashed with the same Argon2 the passwords use.

WHY A ROW RATHER THAN A SIGNED TOKEN. A signed token cannot be revoked, cannot
count attempts, and cannot be rate-limited without server state anyway. All
three of those are the actual security of an OTP: six digits is 1-in-a-million
per guess, which is nothing without an attempt cap.

WHY IT SURVIVES USE. ``consumed_at`` marks a code spent rather than deleting the
row, so a replay is distinguishable from a code that never existed — and so the
next request can see that one was just issued and refuse to flood the seller.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LoginCode(Base):
    """One issued code, and everything needed to refuse a bad attempt."""

    __tablename__ = "login_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: 2547XXXXXXXX. Not a foreign key: a code is issued BEFORE we know whether
    #: an account exists, because verifying the number is what decides whether
    #: this is a sign-in or a sign-up.
    phone: Mapped[str] = mapped_column(String(20), nullable=False)

    #: Argon2 of the six digits. Never the digits themselves.
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    #: Wrong guesses so far. The cap is what makes six digits safe.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Set when the code is successfully used. A spent code is kept, not
    #: deleted — see the module docstring.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="ck_login_codes_attempts_non_negative"),
        # The verify path looks up the newest live code for a number on every
        # attempt, including every wrong one.
        Index("ix_login_codes_phone_created", "phone", "created_at"),
    )

    @property
    def is_expired(self) -> bool:
        """Whether this code is past its lifetime."""
        expires = self.expires_at
        if expires.tzinfo is None:
            # Postgres hands back an aware value; a freshly built object in a
            # test may not be. Comparing naive to aware raises, and this is not
            # the place to discover that.
            expires = expires.replace(tzinfo=UTC)
        return expires < datetime.now(UTC)

    @property
    def is_spent(self) -> bool:
        """Whether it has already been used."""
        return self.consumed_at is not None

    def __repr__(self) -> str:
        return f"<LoginCode phone={self.phone} spent={self.is_spent} attempts={self.attempts}>"
