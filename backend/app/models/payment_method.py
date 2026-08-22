"""
How one seller gets paid — and therefore how their checkout behaves.

    Seller ──1:1──▶ PaymentMethod ──┬──▶ pochi     manual confirmation only
                                    ├──▶ till      STK if credentials given
                                    └──▶ paybill   STK if credentials given

WE ARE NEVER IN THE MONEY PATH. The buyer pays the seller directly. There is no
platform shortcode, no float, no settlement — a deliberate refusal to become a
payment intermediary holding other people's money, which in Kenya is a CBK / PSP
licensing question. The cost of that refusal is that no per-transaction
commission is possible; revenue is the subscription tier.

THE HARD CONSTRAINT THIS TABLE ENCODES. **Daraja's STK Push and C2B APIs work
with Paybill and Buy Goods shortcodes only.** Pochi la Biashara is neither, so a
Pochi seller can never receive an automatic confirmation — not because we
haven't built it, but because Safaricom does not offer it.

A large share of Kenyan micro-sellers use Pochi precisely because it needs no
business registration. So the manual path is permanent and first-class. Any code
that treats STK as the real path and manual as a fallback is wrong for most of
the sellers we have.

ONE METHOD PER SELLER, enforced by a unique constraint. Checkout has to name a
single destination; "which of your three tills did you want the money in?" is
not a question to ask a buyer mid-purchase.

CREDENTIALS ARE ENCRYPTED, NEVER HASHED. We have to send them to Daraja, so they
must be recoverable — see ``app/secrets_vault.py``, which explains what a breach
would cost and why the manual path means nobody is forced to hand them over.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import PaymentMethodKind

if TYPE_CHECKING:
    from app.models.seller import Seller


class PaymentMethod(Base):
    """Where a seller's money goes, and whether we can confirm it automatically."""

    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: UNIQUE. Checkout must resolve to exactly one destination.
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    seller: Mapped[Seller] = relationship(back_populates="payment_method")

    #: pochi | till | paybill
    kind: Mapped[str] = mapped_column(String(20), nullable=False)

    #: What the buyer is told to pay to — a phone number for Pochi, a till or
    #: paybill number otherwise. Shown verbatim at checkout, so it is stored the
    #: way the seller gave it rather than normalised into a shape they would not
    #: recognise on their own statement.
    number: Mapped[str] = mapped_column(String(20), nullable=False)

    #: The name M-Pesa shows the buyer when they pay. Displayed at checkout so a
    #: buyer can confirm the name on the prompt matches before sending money —
    #: the single most effective anti-fraud check available to them.
    account_name: Mapped[str | None] = mapped_column(String(120))

    #: For a paybill that needs one. NULL for Pochi and Buy Goods tills.
    account_reference: Mapped[str | None] = mapped_column(String(40))

    # ── Daraja credentials, all optional and all encrypted ───────────────────
    # Present only when a Till/Paybill seller opts into automatic confirmation.
    # Encrypted with Fernet; never logged, never rendered, never returned by an
    # API. A Postgres dump alone yields nothing — the key is in the environment.

    #: The shortcode STK pushes are made against. Distinct from ``number``: a
    #: seller's public till number and the API shortcode are not always equal.
    stk_shortcode: Mapped[str | None] = mapped_column(String(20))
    consumer_key_enc: Mapped[str | None] = mapped_column(Text)
    consumer_secret_enc: Mapped[str | None] = mapped_column(Text)
    passkey_enc: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('pochi', 'till', 'paybill')",
            name="ck_payment_methods_kind_valid",
        ),
        # RAIL: Pochi cannot carry STK credentials, because Daraja cannot push
        # to it. Storing them would create a row that looks STK-capable and
        # fails at the worst possible moment — with a buyer waiting.
        CheckConstraint(
            "kind <> 'pochi' OR ("
            " stk_shortcode IS NULL AND consumer_key_enc IS NULL"
            " AND consumer_secret_enc IS NULL AND passkey_enc IS NULL)",
            name="ck_payment_methods_pochi_has_no_stk",
        ),
        # RAIL: credentials are all-or-nothing. Three of four is not a usable
        # configuration, and discovering that at the STK call means a buyer sees
        # a failure the seller could have been told about at setup.
        CheckConstraint(
            "(stk_shortcode IS NULL AND consumer_key_enc IS NULL"
            " AND consumer_secret_enc IS NULL AND passkey_enc IS NULL)"
            " OR (stk_shortcode IS NOT NULL AND consumer_key_enc IS NOT NULL"
            " AND consumer_secret_enc IS NOT NULL AND passkey_enc IS NOT NULL)",
            name="ck_payment_methods_stk_credentials_complete",
        ),
    )

    @property
    def kind_enum(self) -> PaymentMethodKind:
        """The kind as its enum, for behaviour that belongs on the type."""
        return PaymentMethodKind(self.kind)

    @property
    def can_stk(self) -> bool:
        """
        Whether this seller's checkout can push an STK prompt.

        Two conditions, and both are load-bearing: the destination must be a
        kind Daraja can reach at all, and the seller must actually have given us
        credentials. The constraints above guarantee the credentials are either
        wholly present or wholly absent, so one NULL check answers it.
        """
        return self.kind_enum.supports_stk and self.passkey_enc is not None

    def __repr__(self) -> str:
        return f"<PaymentMethod seller_id={self.seller_id} kind={self.kind} stk={self.can_stk}>"
