"""
An order, its lines, and the payment attempts against it.

    Cart ──▶ Order ──┬──▶ OrderItem   (prices COPIED, never joined)
                     └──▶ Payment     (STK callback, or a buyer's claim)

EVERYTHING HERE IS A SNAPSHOT, AND THAT IS THE POINT. A cart reads live prices,
which is right while browsing — a seller fixing a typo should change what the
basket shows. An order must not. The moment it is placed, the title, the price,
the units and the destination the money goes to are all copied onto these rows
and stop tracking their source.

The failure this prevents: a seller raises a price on Tuesday and every order
placed on Monday silently re-prices itself. The buyer paid one number and the
record shows another, and there is no way to tell which was true.

THE STATUS MACHINE, and why a boolean was not enough:

    pending ──▶ awaiting_confirmation ──▶ paid
       │              (manual path)         ▲
       │                                    │
       └────────── STK callback ────────────┘
       │
       └──▶ cancelled / failed

``awaiting_confirmation`` means **somebody says they paid and nobody has
checked**. It exists because Pochi la Biashara cannot receive an STK push —
Daraja works with Paybill and Buy Goods only — so for a large share of our
sellers a buyer-entered M-Pesa code is the only signal there is. It is a claim,
not a payment, and only the seller turns it into one.

WE ARE NEVER IN THE MONEY PATH, so there is no ``platform_fee`` and no
``seller_payout`` column here. The buyer pays the seller. We record that it
happened. An earlier draft of this schema had those columns for an aggregator
model that was rejected — see ``docs/BUILD_LOG.md``, 2026-08-22.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import OrderStatus

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.seller import Seller


class Order(Base):
    """One purchase, from one buyer, at one shop."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The human-facing identifier: what the buyer quotes in a WhatsApp message
    #: and what the seller searches for. Unguessable rather than sequential —
    #: an order page reachable by counting would expose strangers' phone
    #: numbers and delivery addresses.
    reference: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    seller: Mapped[Seller] = relationship()

    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=OrderStatus.PENDING.value
    )

    # ── The buyer, as they identified themselves ─────────────────────────────
    # There is no buyer account and there must not be one: requiring a login to
    # buy is the friction this product exists to remove. The webview also has no
    # idea who they are — a link opened from a status is just a browser tab, so
    # none of this can be inferred and all of it has to be asked for.

    buyer_name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: Doubles as the STK push target. One field, two jobs, asked once.
    buyer_phone: Mapped[str] = mapped_column(String(20), nullable=False)

    #: Explicit opt-in for a receipt over WhatsApp. Consent is a checkbox the
    #: buyer ticks, never an inference from "they came from WhatsApp".
    #:
    #: The on-page receipt is the PRIMARY and does not depend on this — it is
    #: the only one that works regardless of opt-in, network, or Meta's template
    #: rules. This governs the bonus, not the record.
    whatsapp_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    delivery_address: Mapped[str | None] = mapped_column(Text)
    delivery_note: Mapped[str | None] = mapped_column(Text)

    # ── Money, integer KES throughout ────────────────────────────────────────
    subtotal_kes: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_fee_kes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_kes: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Where the money was meant to go, copied at order time ────────────────
    # Copied for the same reason prices are: a seller who changes their till
    # next month must not rewrite where last month's orders were paid.

    paid_to_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    paid_to_number: Mapped[str] = mapped_column(String(20), nullable=False)
    paid_to_name: Mapped[str | None] = mapped_column(String(120))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    #: When money was confirmed. NULL until then, and the constraint below keeps
    #: it honest — a paid order without a time is a record nobody can audit.
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Payment.id"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'awaiting_confirmation', 'paid', 'cancelled', 'failed')",
            name="ck_orders_status_valid",
        ),
        CheckConstraint(
            "paid_to_kind IN ('pochi', 'till', 'paybill')",
            name="ck_orders_paid_to_kind_valid",
        ),
        # Money rails. Zero-total orders are a bug, never a gift.
        CheckConstraint("subtotal_kes > 0", name="ck_orders_subtotal_positive"),
        CheckConstraint("delivery_fee_kes >= 0", name="ck_orders_delivery_fee_non_negative"),
        CheckConstraint("total_kes > 0", name="ck_orders_total_positive"),
        # The total must be the sum of its parts. Without this the receipt and
        # the amount charged can disagree, which is the one error a buyer never
        # forgives.
        CheckConstraint(
            "total_kes = subtotal_kes + delivery_fee_kes",
            name="ck_orders_total_is_subtotal_plus_delivery",
        ),
        # A paid order always knows when. Enforced here so no code path can
        # mark money received without recording the moment.
        CheckConstraint(
            "(status = 'paid') = (paid_at IS NOT NULL)",
            name="ck_orders_paid_has_timestamp",
        ),
        # The seller's order list: newest first, per shop.
        Index("ix_orders_seller_created", "seller_id", "created_at"),
        Index("ix_orders_seller_status", "seller_id", "status"),
    )

    @property
    def status_enum(self) -> OrderStatus:
        """The status as its enum, for behaviour that belongs on the type."""
        return OrderStatus(self.status)

    @property
    def item_count(self) -> int:
        """Total units ordered."""
        return sum(item.quantity for item in self.items)

    def __repr__(self) -> str:
        return f"<Order {self.reference} status={self.status} total={self.total_kes}>"


class OrderItem(Base):
    """One line of an order, frozen at the moment it was placed."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    order: Mapped[Order] = relationship(back_populates="items")

    #: SET NULL, not CASCADE, and nullable on purpose. A seller deleting a
    #: product must not delete the record of it having been sold — the snapshot
    #: fields below carry everything a receipt needs, so the line survives its
    #: product.
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    product: Mapped[Product | None] = relationship()

    # ── The snapshot. Never joined back to the product. ──────────────────────
    title: Mapped[str] = mapped_column(String(200), nullable=False)

    #: Price of ONE UNIT AS SOLD at the time of order — which is not always one
    #: item. A bale line is "KES 3,000 for 30 pairs", and the units below are
    #: copied too so the receipt can say so without consulting a live product.
    unit_price_kes: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_quantity: Mapped[int | None] = mapped_column(Integer)
    unit_label: Mapped[str | None] = mapped_column(String(32))

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The buyer's choice as offered: "Size 40 / Black". Empty string when the
    #: product offered none — never NULL, matching ``CartItem``.
    selected_variant: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    #: Copied so a receipt still shows the item after a seller replaces the photo.
    cover_url: Mapped[str | None] = mapped_column(String(500))

    line_total_kes: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price_kes > 0", name="ck_order_items_unit_price_positive"),
        # The arithmetic must hold in the database, not merely in the code that
        # wrote the row. A line whose total is not its price times its count is
        # a receipt that cannot be defended.
        CheckConstraint(
            "line_total_kes = unit_price_kes * quantity",
            name="ck_order_items_line_total_is_price_times_quantity",
        ),
        # Same pairing rule as Product: "3,000 for 30" of what?
        CheckConstraint(
            "(unit_quantity IS NULL) = (unit_label IS NULL)",
            name="ck_order_items_unit_quantity_and_label_together",
        ),
        Index("ix_order_items_order", "order_id"),
    )

    @property
    def price_display(self) -> str:
        """The line's unit price as the buyer saw it, units and all."""
        price = f"KES {self.unit_price_kes:,}"
        if self.unit_quantity and self.unit_quantity > 1 and self.unit_label:
            return f"{price} for {self.unit_quantity} {self.unit_label}"
        return price

    def __repr__(self) -> str:
        return f"<OrderItem {self.title!r} x{self.quantity} @ {self.unit_price_kes}>"


class Payment(Base):
    """
    One attempt to pay an order — automatic or claimed.

    TWO SHAPES IN ONE TABLE, because they answer the same question: did money
    arrive? An STK attempt carries Daraja's ids and its callback; a manual claim
    carries the code a buyer typed and the seller who vouched for it.

    IDEMPOTENCY LIVES ON ``checkout_request_id``. Daraja retries its callback,
    and a retry that creates a second payment row would let one purchase read as
    two. The unique constraint makes the duplicate impossible at the storage
    layer rather than relying on every handler to check first.
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    order: Mapped[Order] = relationship(back_populates="payments")

    #: pochi | till | paybill — copied from the order, so a payment row explains
    #: itself without a join.
    method_kind: Mapped[str] = mapped_column(String(20), nullable=False)

    amount_kes: Mapped[int] = mapped_column(Integer, nullable=False)

    #: The number the prompt went to, or that the buyer says they paid from.
    phone: Mapped[str | None] = mapped_column(String(20))

    # ── The STK path ─────────────────────────────────────────────────────────
    #: Daraja's id for the push. UNIQUE, and the whole idempotency story.
    checkout_request_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    merchant_request_id: Mapped[str | None] = mapped_column(String(64))
    result_code: Mapped[int | None] = mapped_column(Integer)
    result_desc: Mapped[str | None] = mapped_column(Text)

    #: The callback exactly as received. Kept verbatim because when a payment is
    #: disputed this is the only evidence, and a parsed summary is an opinion
    #: about it. JSONB so it can be queried without being re-parsed.
    raw_callback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # ── The manual path ──────────────────────────────────────────────────────
    #: The M-Pesa confirmation code the BUYER typed. Unverified text until a
    #: human checks it: this is a claim, and naming the column for what it is
    #: keeps anyone from treating it as proof.
    claimed_code: Mapped[str | None] = mapped_column(String(20))

    #: When the seller confirmed the claim. NULL means nobody has checked.
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: The M-Pesa receipt as CONFIRMED — from a callback, or the seller
    #: accepting a claim. Distinct from ``claimed_code`` on purpose: one is what
    #: someone said, the other is what was established.
    mpesa_receipt: Mapped[str | None] = mapped_column(String(20))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "method_kind IN ('pochi', 'till', 'paybill')",
            name="ck_payments_method_kind_valid",
        ),
        CheckConstraint("amount_kes > 0", name="ck_payments_amount_positive"),
        # A confirmed payment names what confirmed it. Without this, a row can
        # claim money arrived while pointing at no evidence at all.
        CheckConstraint(
            "confirmed_at IS NULL OR mpesa_receipt IS NOT NULL",
            name="ck_payments_confirmed_has_receipt",
        ),
        UniqueConstraint("checkout_request_id", name="uq_payments_checkout_request"),
        Index("ix_payments_order", "order_id"),
    )

    @property
    def is_confirmed(self) -> bool:
        """Whether this attempt actually resulted in money."""
        return self.confirmed_at is not None

    def __repr__(self) -> str:
        return f"<Payment order_id={self.order_id} confirmed={self.is_confirmed}>"
