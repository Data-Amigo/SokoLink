"""
The buyer's basket: getting one, changing it, and refusing what it must not hold.

    cookie token ──▶ get_or_create_cart(seller) ──▶ Cart
                                                     │
    add_item / set_quantity / remove_item ───────────┘

WHY THE RULES LIVE HERE AND NOT IN THE ROUTE. Every one of them is a rule about
what a basket may contain — that a product belongs to this shop, that it is
published, that the quantity is sane. A route that built its own query would
have to remember all of them, and the one that forgets is the one that lets a
buyer add another seller's product to this seller's basket and pay the wrong
person.

THE CART IS SCOPED TO ONE SELLER, AND THAT IS LOAD-BEARING. Money goes straight
from the buyer to the seller, so a basket spanning two shops is two payments
wearing one Checkout button. ``add_item`` re-checks the product's seller on
every call rather than trusting the caller's URL.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It never copies a price. A cart line
reads its product's price live, which is right while browsing: a seller fixing
a typo should change what the basket shows. Copying happens exactly once, when
an order is placed, and that is ``services/orders.py``'s job — which is why
``OrderItem`` is a separate table rather than a flag on ``CartItem``.
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Cart, CartItem, Product, ProductStatus, Seller

#: The buyer's cookie. Not the session cookie: a buyer is not logged in and must
#: never be — requiring an account to buy is the friction this product exists to
#: remove.
CART_COOKIE = "cart"

#: 32 bytes of URL-safe randomness. This token is the ONLY thing standing
#: between a stranger and someone else's basket and delivery address, so it is
#: generated the way a password reset token is, not with an incrementing id.
_TOKEN_BYTES = 32

#: One line cannot exceed this. A basket asking for 900 of anything is a fat
#: finger or a bot, and either way the seller does not have 900.
MAX_QUANTITY_PER_LINE = 99


class CartError(Exception):
    """A basket operation was refused, with a message safe to show a buyer."""


def new_token() -> str:
    """A fresh, unguessable cart token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def get_cart(db: Session, token: str | None, seller: Seller) -> Cart | None:
    """
    Load the basket this token names, if it belongs to this shop.

    Args:
        db: Session.
        token: The cookie value, or None when the buyer has no cookie yet.
        seller: The shop being browsed.

    Returns:
        The cart, or None when there is no token, no such cart, or the cart
        belongs to a different shop.

    Notes:
        The seller check is what stops one shop's page rendering another shop's
        basket when a buyer moves between two storefronts in one browser. It is
        a mismatch rather than an error: the buyer simply gets a new basket
        here, and the old one is untouched for when they go back.
    """
    if not token:
        return None

    return db.scalar(
        select(Cart)
        .where(Cart.token == token, Cart.seller_id == seller.id)
        .options(selectinload(Cart.items).selectinload(CartItem.product))
    )


def get_or_create_cart(db: Session, token: str | None, seller: Seller) -> Cart:
    """
    The basket for this buyer at this shop, creating one if needed.

    Args:
        db: Session.
        token: The cookie value, if any.
        seller: The shop.

    Returns:
        A persisted cart. The caller must write ``cart.token`` back to the
        buyer's cookie — this function cannot, because it has no response.
    """
    cart = get_cart(db, token, seller)
    if cart is not None:
        return cart

    cart = Cart(token=new_token(), seller_id=seller.id)
    db.add(cart)
    db.flush()
    return cart


def _purchasable(db: Session, seller: Seller, product_id: int) -> Product:
    """
    The product, if a buyer is allowed to put it in this shop's basket.

    Raises:
        CartError: When the product does not exist, belongs to another seller,
            or is not published. All three give the SAME message: distinguishing
            them would tell a stranger which product ids exist in which shop.
    """
    product = db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.seller_id == seller.id,
            Product.status == ProductStatus.PUBLISHED.value,
        )
    )
    if product is None:
        raise CartError("That item is no longer available.")
    return product


def add_item(
    db: Session,
    cart: Cart,
    product_id: int,
    quantity: int = 1,
    selected_variant: str = "",
) -> CartItem:
    """
    Put something in the basket, or add to a line that is already there.

    Args:
        db: Session.
        cart: The basket.
        product_id: What to add.
        quantity: How many. Must be positive.
        selected_variant: The buyer's size/colour choice, as offered. Empty
            string when the product offered no choice — never None, because the
            uniqueness of a line depends on it and Postgres treats NULLs as
            distinct.

    Returns:
        The created or updated line.

    Raises:
        CartError: If the product is not purchasable here, or the quantity is
            out of range.
    """
    if quantity < 1:
        raise CartError("Choose at least one.")

    product = _purchasable(db, cart.seller, product_id)
    variant = (selected_variant or "").strip()

    # Adding the same product and choice twice increments the existing line.
    # Two rows for one shoe makes a buyer distrust the total more than the
    # duplicate itself ever cost.
    existing = db.scalar(
        select(CartItem).where(
            CartItem.cart_id == cart.id,
            CartItem.product_id == product.id,
            CartItem.selected_variant == variant,
        )
    )

    wanted = (existing.quantity if existing else 0) + quantity
    if wanted > MAX_QUANTITY_PER_LINE:
        raise CartError(f"You can order at most {MAX_QUANTITY_PER_LINE} of one item.")

    if existing is not None:
        existing.quantity = wanted
        db.flush()
        return existing

    item = CartItem(
        cart_id=cart.id,
        product_id=product.id,
        quantity=quantity,
        selected_variant=variant,
    )
    db.add(item)
    db.flush()
    return item


def set_quantity(db: Session, cart: Cart, item_id: int, quantity: int) -> None:
    """
    Change how many of one line, or remove it when the quantity reaches zero.

    Args:
        db: Session.
        cart: The basket the line must belong to.
        item_id: Which line.
        quantity: The new count. Zero or less removes the line.

    Raises:
        CartError: If the line is not in this basket, or the quantity is too
            large. Scoping to the cart matters: line ids are sequential, and
            without it a guessed id would edit a stranger's basket.
    """
    if quantity > MAX_QUANTITY_PER_LINE:
        raise CartError(f"You can order at most {MAX_QUANTITY_PER_LINE} of one item.")

    item = db.scalar(select(CartItem).where(CartItem.id == item_id, CartItem.cart_id == cart.id))
    if item is None:
        raise CartError("That item is not in your basket.")

    if quantity < 1:
        db.delete(item)
    else:
        item.quantity = quantity
    db.flush()


def remove_item(db: Session, cart: Cart, item_id: int) -> None:
    """Take one line out of the basket."""
    set_quantity(db, cart, item_id, 0)


def clear(db: Session, cart: Cart) -> None:
    """
    Empty the basket, keeping the cart row and its token.

    Called after an order is placed. The row survives so the buyer's cookie
    stays valid and their next visit does not start by minting a new cart.

    Notes:
        THE COLLECTION IS EXPIRED, not just flushed. Deleting the rows does not
        empty ``cart.items`` on an instance the session has already loaded, and
        the session runs with autoflush off — so the very next read handed back
        the basket that had just been emptied.

        In the chat that meant "clear" answered "Basket emptied" and then showed
        every item still in it. It went unnoticed because the test asserted the
        word "empty" appeared, and the basket's own boilerplate said "clear to
        empty it" — so the assertion passed on the instructions rather than on
        the basket.
    """
    for item in list(cart.items):
        db.delete(item)
    db.flush()
    db.expire(cart, ["items"])


def unavailable_lines(cart: Cart) -> list[CartItem]:
    """
    Lines that can no longer be bought, for a warning above the Checkout button.

    A basket can sit for days. In that time a seller may unpublish an item or
    sell the last one, and a buyer who only discovers that *after* an M-Pesa
    prompt has been asked for money under false pretences.

    Returns:
        Lines whose product is unpublished, unpriced, or out of stock.
    """
    stale = []
    for item in cart.items:
        product = item.product
        if (
            product is None
            or product.status != ProductStatus.PUBLISHED.value
            or product.price_kes is None
            or product.stock < 1
        ):
            stale.append(item)
    return stale
