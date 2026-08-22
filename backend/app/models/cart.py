"""
A buyer's basket, before it becomes an order.

    browser cookie ──▶ Cart ──▶ CartItem ──▶ Product
                        │
                        └──▶ Seller   (exactly one — see below)

WHY A TABLE RATHER THAN A COOKIE. The obvious cheap answer is to keep the basket
in a signed cookie and skip a table entirely. It fails on the surface we care
most about: the WhatsApp in-app browser, on a phone, on a patchy connection. A
cookie basket dies when the buyer is bounced out to M-Pesa and back, and a
buyer who loses their basket at the payment step does not rebuild it — they
leave. The server keeps it; the cookie holds nothing but an opaque token.

WHY A CART BELONGS TO EXACTLY ONE SELLER. This is not a marketplace basket.
**Money goes directly from the buyer to the seller**, so a basket spanning two
shops would be two payments, two M-Pesa numbers and two confirmations wearing
one Checkout button. Scoping the cart to a seller at the schema level means no
checkout code has to discover that halfway through and fail gracefully.

WHY THE TOKEN IS NOT THE PRIMARY KEY. Cart ids appear in server logs and in
HTMX request paths; the token is the bearer credential that grants access to
somebody's basket and delivery details. Keeping them separate means a leaked id
is not a leaked cart.

NOTHING HERE IS A PRICE RECORD. A cart line points at a product and reads its
price live, which is correct while browsing — a seller fixing a typo should
change what the basket shows. The moment an order is placed the price is
COPIED onto the order line and stops tracking. That transition is the whole
reason ``OrderItem`` is a separate table rather than a flag on this one.
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
    from app.models.product import Product
    from app.models.seller import Seller


class Cart(Base):
    """One buyer's basket at one shop."""

    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: The opaque value held in the buyer's cookie. Unguessable, because holding
    #: it is the only thing that proves the basket is yours — there is no login
    #: on the buyer side and there must not be one.
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    #: The shop this basket belongs to. One seller per cart, always.
    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    seller: Mapped[Seller] = relationship()

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart",
        cascade="all, delete-orphan",
        order_by="CartItem.id",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Touched on every change, so abandoned baskets can be swept later without
    #: guessing from ``created_at`` whether anyone is still shopping.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_carts_seller", "seller_id"),)

    @property
    def item_count(self) -> int:
        """Total units in the basket — the number on the cart badge."""
        return sum(item.quantity for item in self.items)

    @property
    def subtotal_kes(self) -> int:
        """
        Sum of the lines, in whole KES.

        Lines whose product has no price contribute nothing rather than raising.
        An unpriced product cannot be published and so cannot reach a basket,
        but a subtotal is not the place to discover that a rail leaked.
        """
        return sum(item.line_total_kes for item in self.items)

    def __repr__(self) -> str:
        return f"<Cart id={self.id} seller_id={self.seller_id} items={len(self.items)}>"


class CartItem(Base):
    """One product, one chosen variant, one quantity."""

    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), nullable=False)
    cart: Mapped[Cart] = relationship(back_populates="items")

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    product: Mapped[Product] = relationship()

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: The buyer's choice, exactly as offered: "Size 40 / Black", "one size".
    #:
    #: A STRING, not a foreign key to a variant table, and that is a decision
    #: rather than a shortcut. Kenyan micro-sellers hold a pile of stock and
    #: sell until it is gone; they do not count "Black 38" apart from
    #: "Black 40". Modelling per-variant stock would impose an inventory
    #: discipline the seller does not keep, and every count would drift from
    #: reality within a week.
    #:
    #: EMPTY STRING, NOT NULL, when the product offered no choice. Postgres
    #: treats NULLs as distinct in a unique index, so a nullable column here
    #: would let the same no-variant product be added twice as two separate
    #: rows — exactly the duplicate the constraint below exists to prevent.
    selected_variant: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_cart_items_quantity_positive"),
        # Adding the same thing twice increments the line rather than creating a
        # second one — otherwise a buyer tapping Add twice sees their basket
        # list the same shoe in two rows and distrusts the total.
        UniqueConstraint(
            "cart_id",
            "product_id",
            "selected_variant",
            name="uq_cart_items_line",
        ),
    )

    @property
    def line_total_kes(self) -> int:
        """Quantity times the product's current price. Zero when unpriced."""
        price = self.product.price_kes if self.product is not None else None
        return (price or 0) * self.quantity

    def __repr__(self) -> str:
        return (
            f"<CartItem id={self.id} product_id={self.product_id} "
            f"qty={self.quantity} variant={self.selected_variant!r}>"
        )
