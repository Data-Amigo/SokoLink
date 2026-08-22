"""
Turning a basket into an order, and an order into a confirmed payment.

    Cart ──▶ place_order() ──▶ Order(pending) ──┬──▶ claim_payment()
                                                │        │
                                                │   awaiting_confirmation
                                                │        │
                                                │   confirm_payment()  ← SELLER
                                                │        │
                                                └────────▶ Order(paid)

THE COPY IS THE WHOLE JOB. A cart reads live prices; an order must not. Every
title, price, unit and payment destination is copied onto the order rows here,
once, and never consulted again. A seller raising a price on Tuesday must not
silently re-price Monday's orders.

STOCK IS DECREMENTED AT PLACEMENT, NOT AT PAYMENT, and that is a trade rather
than an oversight. On the manual path a seller may take hours to check their
phone, and leaving stock available for that whole window is how two buyers pay
for the last pair of shoes. The cost is that an abandoned order holds stock
until it is cancelled, which for a seller with five items is visible and fixable;
overselling is neither. ``cancel_order`` puts it back.

ONLY THE SELLER MOVES AN ORDER TO PAID on the manual path. A buyer-entered
M-Pesa code is a claim — unverified text that looks exactly like a real one.
``claim_payment`` records it and stops. That distinction is the difference
between a receipt and a guess.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Cart,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentMethod,
    Seller,
)
from app.services.cart import clear as clear_cart
from app.services.cart import unavailable_lines

#: Reference alphabet: unambiguous when read aloud down a phone line or copied
#: off a cracked screen. No 0/O, no 1/I/L — a buyer quoting their reference to a
#: seller in a WhatsApp message must not be defeated by a font.
_REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_REFERENCE_LENGTH = 8


class OrderError(Exception):
    """An order could not be placed or advanced, with a buyer-safe message."""


def _new_reference() -> str:
    """
    A short, unguessable order reference.

    NOT sequential. An order page reachable by counting would hand a stranger
    every buyer's phone number and delivery address in turn. 8 characters of
    this alphabet is ~40 bits, which is far beyond guessing at our volume while
    staying short enough to read out loud.
    """
    body = "".join(secrets.choice(_REFERENCE_ALPHABET) for _ in range(_REFERENCE_LENGTH))
    return f"BM-{body}"


def get_payment_method(db: Session, seller: Seller) -> PaymentMethod | None:
    """The seller's single payment destination, or None if they never set one."""
    return db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id))


def place_order(
    db: Session,
    cart: Cart,
    *,
    buyer_name: str,
    buyer_phone: str,
    delivery_address: str | None = None,
    delivery_note: str | None = None,
    whatsapp_opt_in: bool = False,
    delivery_fee_kes: int = 0,
) -> Order:
    """
    Freeze a basket into an order and empty the basket.

    Args:
        db: Session.
        cart: The basket to convert. Must belong to the seller being paid.
        buyer_name: As the buyer typed it.
        buyer_phone: The M-Pesa line. Doubles as the STK target on that path.
        delivery_address: Where it goes, if the buyer said.
        delivery_note: Anything else they added.
        whatsapp_opt_in: Explicit consent for a WhatsApp receipt. Never inferred.
        delivery_fee_kes: Added to the total. Zero when arranged with the seller.

    Returns:
        A persisted ``Order`` in ``PENDING``.

    Raises:
        OrderError: If the basket is empty, holds something no longer
            purchasable, or the seller has no payment method configured. The
            last is checked HERE rather than at the payment step: taking a
            buyer's details and then discovering there is nowhere to send money
            wastes their time and looks broken.
    """
    if not cart.items:
        raise OrderError("Your basket is empty.")

    stale = unavailable_lines(cart)
    if stale:
        raise OrderError("Some items are no longer available. Please review your basket.")

    if not buyer_name.strip():
        raise OrderError("Please enter your name.")
    if not buyer_phone.strip():
        raise OrderError("Please enter the phone number you will pay from.")
    if delivery_fee_kes < 0:
        raise OrderError("Delivery cannot be negative.")

    method = get_payment_method(db, cart.seller)
    if method is None:
        raise OrderError("This shop has not set up payments yet. Message the seller to order.")

    # Totals are computed BEFORE the order row is created, not patched onto it
    # afterwards. ``ck_orders_subtotal_positive`` rejects a zero-total order at
    # INSERT, and a flush placing a placeholder row would hit it — correctly.
    # The rail is right; the order of operations has to respect it.
    lines: list[dict[str, Any]] = []
    subtotal = 0
    for line in cart.items:
        product = line.product
        # Guarded by unavailable_lines above; re-checked here because the price
        # is about to become a number someone pays.
        if product is None or product.price_kes is None:
            raise OrderError("Some items are no longer available. Please review your basket.")

        line_total = product.price_kes * line.quantity
        subtotal += line_total
        lines.append(
            {
                "product": product,
                "title": product.title,
                "unit_price_kes": product.price_kes,
                "unit_quantity": product.unit_quantity,
                "unit_label": product.unit_label,
                "quantity": line.quantity,
                "selected_variant": line.selected_variant,
                "cover_url": product.cover_url,
                "line_total_kes": line_total,
            }
        )

    order = Order(
        reference=_new_reference(),
        seller_id=cart.seller_id,
        status=OrderStatus.PENDING.value,
        buyer_name=buyer_name.strip(),
        buyer_phone=buyer_phone.strip(),
        whatsapp_opt_in=whatsapp_opt_in,
        delivery_address=(delivery_address or "").strip() or None,
        delivery_note=(delivery_note or "").strip() or None,
        subtotal_kes=subtotal,
        delivery_fee_kes=delivery_fee_kes,
        total_kes=subtotal + delivery_fee_kes,
        # Copied, not joined: a seller changing their till next month must not
        # rewrite where this order was paid.
        paid_to_kind=method.kind,
        paid_to_number=method.number,
        paid_to_name=method.account_name,
    )
    db.add(order)
    db.flush()

    for values in lines:
        product = values.pop("product")
        db.add(OrderItem(order_id=order.id, product_id=product.id, **values))

        # Reserve the stock. See the module docstring for why this happens now
        # rather than on payment.
        product.stock = max(0, product.stock - values["quantity"])

    clear_cart(db, cart)
    db.flush()
    return order


def get_order(db: Session, reference: str) -> Order | None:
    """
    Load an order by its public reference.

    The reference is the only credential: it is unguessable, and holding it is
    what proves the order is yours. There is no buyer account to check against.
    """
    return db.scalar(select(Order).where(Order.reference == reference))


def claim_payment(db: Session, order: Order, code: str) -> Payment:
    """
    Record a buyer's assertion that they have paid.

    **This does not mark the order paid, and must not.** The code is text the
    buyer typed; it looks identical whether it came off a real M-Pesa message or
    was invented. Only the seller, looking at their own phone, can turn it into
    a payment.

    Args:
        db: Session.
        order: The order being claimed against.
        code: The M-Pesa confirmation code, as entered.

    Returns:
        The recorded ``Payment`` attempt.

    Raises:
        OrderError: If the order is already settled or the code is blank.
    """
    if order.status_enum.is_final:
        raise OrderError("This order is already closed.")

    cleaned = code.strip().upper()
    if not cleaned:
        raise OrderError("Please enter the M-Pesa confirmation code.")

    payment = Payment(
        order_id=order.id,
        method_kind=order.paid_to_kind,
        amount_kes=order.total_kes,
        phone=order.buyer_phone,
        claimed_code=cleaned,
    )
    db.add(payment)

    order.status = OrderStatus.AWAITING_CONFIRMATION.value
    db.flush()
    return payment


def confirm_payment(db: Session, order: Order, receipt: str | None = None) -> Order:
    """
    The seller vouches that the money arrived. This is what makes an order paid.

    Args:
        db: Session.
        order: The order to settle.
        receipt: The confirmed M-Pesa receipt. Defaults to the buyer's claimed
            code when the seller is accepting that claim as-is.

    Returns:
        The order, now ``PAID``.

    Raises:
        OrderError: If the order is already final, or nothing identifies the
            payment. The database enforces the second rule too — a confirmed
            payment without a receipt is a claim that money arrived while
            pointing at no evidence.
    """
    if order.status_enum.is_final:
        raise OrderError("This order is already closed.")

    payment = order.payments[-1] if order.payments else None
    confirmed_receipt = (receipt or "").strip().upper() or (
        payment.claimed_code if payment else None
    )
    if not confirmed_receipt:
        raise OrderError("Enter the M-Pesa code you received before confirming.")

    now = datetime.now(UTC)
    if payment is None:
        payment = Payment(
            order_id=order.id,
            method_kind=order.paid_to_kind,
            amount_kes=order.total_kes,
            phone=order.buyer_phone,
        )
        db.add(payment)

    payment.mpesa_receipt = confirmed_receipt
    payment.confirmed_at = now

    order.status = OrderStatus.PAID.value
    order.paid_at = now
    db.flush()
    return order


def cancel_order(db: Session, order: Order) -> Order:
    """
    Abandon an order and give the stock back.

    Returning stock is the other half of reserving it at placement. Without
    this, every abandoned basket permanently shrinks what the seller can sell.

    Raises:
        OrderError: If the order is already final. A paid order is cancelled by
            refunding, which is a conversation between buyer and seller — we are
            not in the money path and cannot reverse anything.
    """
    if order.status_enum.is_final:
        raise OrderError("This order is already closed.")

    for item in order.items:
        if item.product is not None:
            item.product.stock += item.quantity

    order.status = OrderStatus.CANCELLED.value
    db.flush()
    return order
