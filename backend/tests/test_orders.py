"""
Orders: the copy, the stock reservation, and the claim/confirm distinction.

Three rules carry the weight here, and each has cost somebody real money in
somebody's business:

    an order line COPIES its price — a seller's later edit must not re-price it
    stock is reserved at placement — two buyers must not pay for the last pair
    a buyer's M-Pesa code is a CLAIM — only the seller turns it into a payment

The third is the one to defend hardest. On Pochi la Biashara there is no
callback and never will be, so the only thing standing between "someone typed
ten characters" and "money arrived" is the seller checking their own phone.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import OrderStatus, Product, ProductStatus, Seller
from app.services.cart import CART_COOKIE, add_item, get_or_create_cart
from app.services.orders import (
    OrderError,
    cancel_order,
    claim_payment,
    confirm_payment,
    get_order,
    place_order,
)
from tests.factories import make_payment_method, make_product, make_seller


def shop(db: Session, **overrides: Any) -> Seller:
    """A published shop that can take money."""
    seller = make_seller(db, **overrides)
    seller.is_published = True
    db.flush()
    make_payment_method(db, seller)
    return seller


def item(db: Session, seller: Seller, **overrides: Any) -> Product:
    """A published, priced, in-stock product."""
    values: dict[str, Any] = {
        "price_kes": 1500,
        "stock": 5,
        "status": ProductStatus.PUBLISHED.value,
    }
    values.update(overrides)
    return make_product(db, seller, **values)


def basket(db: Session, seller: Seller, product: Product, quantity: int = 1) -> Any:
    """A basket holding one line."""
    cart = get_or_create_cart(db, None, seller)
    add_item(db, cart, product.id, quantity=quantity)
    return cart


class TestPlacingAnOrder:
    def test_the_price_is_copied_not_joined(self, db: Session) -> None:
        """
        THE ONE THAT MATTERS MOST.

        If the line read through to the product, a seller raising a price on
        Tuesday would silently re-price Monday's orders. The buyer paid one
        number and the record would show another, with no way to tell which was
        true.
        """
        seller = shop(db)
        product = item(db, seller, price_kes=1500)
        order = place_order(db, basket(db, seller, product), buyer_name="A", buyer_phone="0712")

        product.price_kes = 9999
        db.flush()

        assert order.items[0].unit_price_kes == 1500
        assert order.total_kes == 1500

    def test_the_units_are_copied_too(self, db: Session) -> None:
        """A bale line must still read "for 30 pairs" on a receipt months later."""
        seller = shop(db)
        product = item(db, seller, price_kes=3000, unit_quantity=30, unit_label="pairs")
        order = place_order(db, basket(db, seller, product), buyer_name="A", buyer_phone="0712")

        assert order.items[0].price_display == "KES 3,000 for 30 pairs"

    def test_stock_is_reserved_at_placement(self, db: Session) -> None:
        """
        Not at payment. On the manual path a seller may take hours to check
        their phone, and leaving stock available that whole time is how two
        buyers pay for the last pair of shoes.
        """
        seller = shop(db)
        product = item(db, seller, stock=5)
        place_order(db, basket(db, seller, product, quantity=2), buyer_name="A", buyer_phone="0712")

        assert product.stock == 3

    def test_the_payment_destination_is_copied(self, db: Session) -> None:
        """A seller changing their till must not rewrite where old orders were paid."""
        seller = shop(db)
        product = item(db, seller)
        order = place_order(db, basket(db, seller, product), buyer_name="A", buyer_phone="0712")

        assert order.paid_to_kind == "pochi"
        assert order.paid_to_number == "254712345678"

    def test_the_basket_is_emptied(self, db: Session) -> None:
        seller = shop(db)
        product = item(db, seller)
        cart = basket(db, seller, product)
        place_order(db, cart, buyer_name="A", buyer_phone="0712")

        db.refresh(cart)
        assert cart.items == []

    def test_a_shop_with_no_payment_method_cannot_take_an_order(self, db: Session) -> None:
        """
        Refused BEFORE the buyer's details are taken. Collecting a name, a
        phone and an address and only then admitting there is nowhere to send
        money wastes their time and looks broken.
        """
        seller = make_seller(db, slug="nopay")
        seller.is_published = True
        db.flush()
        product = item(db, seller)

        with pytest.raises(OrderError, match="not set up payments"):
            place_order(db, basket(db, seller, product), buyer_name="A", buyer_phone="0712")

    def test_an_empty_basket_is_refused(self, db: Session) -> None:
        seller = shop(db)
        cart = get_or_create_cart(db, None, seller)

        with pytest.raises(OrderError):
            place_order(db, cart, buyer_name="A", buyer_phone="0712")

    def test_an_unavailable_item_blocks_the_order(self, db: Session) -> None:
        """
        A basket can sit for days. Being asked for money for something already
        gone is the worst moment to find out.
        """
        seller = shop(db)
        product = item(db, seller)
        cart = basket(db, seller, product)

        product.stock = 0
        db.flush()
        db.refresh(cart)

        with pytest.raises(OrderError, match="no longer available"):
            place_order(db, cart, buyer_name="A", buyer_phone="0712")

    def test_the_reference_is_not_sequential(self, db: Session) -> None:
        """
        An order page reachable by counting would hand a stranger every buyer's
        phone number and delivery address in turn.
        """
        seller = shop(db)
        first = place_order(
            db,
            basket(db, seller, item(db, seller, platform_post_id="a")),
            buyer_name="A",
            buyer_phone="0712",
        )
        second = place_order(
            db,
            basket(db, seller, item(db, seller, platform_post_id="b")),
            buyer_name="B",
            buyer_phone="0713",
        )

        assert first.reference != second.reference
        assert first.reference.startswith("BM-")
        # No 0/O or 1/I/L: a reference read down a phone line must be unambiguous.
        assert not set("01OIL") & set(first.reference[3:])

    def test_consent_is_off_unless_asked_for(self, db: Session) -> None:
        """Opt-in is a checkbox, never an inference from "they came from WhatsApp"."""
        seller = shop(db)
        product = item(db, seller)
        order = place_order(db, basket(db, seller, product), buyer_name="A", buyer_phone="0712")

        assert order.whatsapp_opt_in is False


class TestClaimingAndConfirming:
    def test_a_claim_does_not_make_an_order_paid(self, db: Session) -> None:
        """
        THE DISTINCTION THE WHOLE MANUAL PATH RESTS ON. The code is text the
        buyer typed and looks identical whether it came off a real M-Pesa
        message or was invented.
        """
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )

        claim_payment(db, order, "slk7xa2b9c")

        assert order.status == OrderStatus.AWAITING_CONFIRMATION.value
        assert order.paid_at is None
        assert order.payments[0].claimed_code == "SLK7XA2B9C"
        assert order.payments[0].mpesa_receipt is None
        assert order.payments[0].is_confirmed is False

    def test_the_seller_confirming_is_what_settles_it(self, db: Session) -> None:
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )
        claim_payment(db, order, "SLK7XA2B9C")

        confirm_payment(db, order)

        assert order.status == OrderStatus.PAID.value
        assert order.paid_at is not None
        assert order.payments[0].mpesa_receipt == "SLK7XA2B9C"
        assert order.payments[0].is_confirmed is True

    def test_a_blank_code_is_refused(self, db: Session) -> None:
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )

        with pytest.raises(OrderError):
            claim_payment(db, order, "   ")

    def test_a_settled_order_cannot_be_claimed_against_again(self, db: Session) -> None:
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )
        claim_payment(db, order, "CODE1")
        confirm_payment(db, order)

        with pytest.raises(OrderError, match="already closed"):
            claim_payment(db, order, "CODE2")

    def test_confirming_needs_something_to_confirm(self, db: Session) -> None:
        """
        A paid order that names no receipt is a claim that money arrived while
        pointing at no evidence. The database refuses it too.
        """
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )

        with pytest.raises(OrderError):
            confirm_payment(db, order)


class TestCancelling:
    def test_cancelling_returns_the_stock(self, db: Session) -> None:
        """The other half of reserving it. Otherwise every abandoned basket
        permanently shrinks what the seller can sell."""
        seller = shop(db)
        product = item(db, seller, stock=5)
        order = place_order(
            db, basket(db, seller, product, quantity=2), buyer_name="A", buyer_phone="0712"
        )
        assert product.stock == 3

        cancel_order(db, order)

        assert product.stock == 5
        assert order.status == OrderStatus.CANCELLED.value

    def test_a_paid_order_cannot_be_cancelled(self, db: Session) -> None:
        """We are not in the money path and cannot reverse anything."""
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )
        claim_payment(db, order, "CODE")
        confirm_payment(db, order)

        with pytest.raises(OrderError):
            cancel_order(db, order)


class TestTheCheckoutRoutes:
    def test_checkout_places_an_order_and_redirects_to_it(
        self, client: TestClient, db: Session
    ) -> None:
        seller = shop(db)
        product = item(db, seller)
        cart = basket(db, seller, product)
        client.cookies.set(CART_COOKIE, cart.token)

        response = client.post(
            f"/shop/{seller.slug}/checkout",
            data={"buyer_name": "Amina", "buyer_phone": "0712345678"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "/order/BM-" in response.headers["location"]

    def test_an_order_from_another_shop_is_a_404(self, client: TestClient, db: Session) -> None:
        """One seller's URL must never surface another's buyer details."""
        mine = shop(db, slug="mine")
        theirs = shop(db, slug="theirs", display_name="Theirs")
        order = place_order(
            db, basket(db, theirs, item(db, theirs)), buyer_name="A", buyer_phone="0712"
        )

        assert client.get(f"/shop/{mine.slug}/order/{order.reference}").status_code == 404

    def test_the_pending_page_shows_where_to_pay(self, client: TestClient, db: Session) -> None:
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )

        body = client.get(f"/shop/{seller.slug}/order/{order.reference}").text
        assert "254712345678" in body
        assert "Pochi la Biashara" in body

    def test_claiming_moves_the_order_but_not_to_paid(
        self, client: TestClient, db: Session
    ) -> None:
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )

        client.post(
            f"/shop/{seller.slug}/order/{order.reference}/claim",
            data={"mpesa_code": "SLK7XA2B9C"},
            follow_redirects=False,
        )

        db.refresh(order)
        assert order.status == OrderStatus.AWAITING_CONFIRMATION.value

    def test_a_paid_order_renders_a_receipt(self, client: TestClient, db: Session) -> None:
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )
        claim_payment(db, order, "SLK7XA2B9C")
        confirm_payment(db, order)

        body = client.get(f"/shop/{seller.slug}/order/{order.reference}").text
        assert "Payment received" in body

    def test_an_order_is_found_by_reference(self, db: Session) -> None:
        seller = shop(db)
        order = place_order(
            db, basket(db, seller, item(db, seller)), buyer_name="A", buyer_phone="0712"
        )

        assert get_order(db, order.reference) is not None
        assert get_order(db, "BM-NOTREAL") is None
