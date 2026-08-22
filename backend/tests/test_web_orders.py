"""
The seller's orders screen — where money is actually confirmed.

``test_orders.py`` covers the service: the copy, the stock reservation, the
claim/confirm distinction. This covers the HTTP shell, where a different set of
things go wrong:

  **Order references travel.** A reference is quoted in a buyer's WhatsApp
  message, so it is not a secret between sellers. Every route that takes one
  must scope it to the signed-in seller's shop, or one seller can read — or
  confirm, or cancel — another's orders.

  **Confirming is the money action.** It is the only thing that turns a claim
  into a payment on the manual path, so it must be POST-only and must never be
  reachable by a stranger.

  **The login wall must cover all of it.** An orders page listing buyers' phone
  numbers and delivery addresses is the last place to forget it.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, OrderStatus, Product, ProductStatus, Seller
from app.services.accounts import create_account
from app.services.cart import add_item, get_or_create_cart
from app.services.orders import claim_payment, place_order
from tests.factories import make_payment_method, make_product, make_seller

PASSWORD = "correct-horse-battery"


def signed_in(client: TestClient, db: Session, email: str = "seller@example.com") -> Account:
    """Create an account with a shop, and start a browser session as it."""
    account = create_account(db, email=email, password=PASSWORD, shop_name="Nairobi Thrift")
    db.flush()
    client.post("/login", data={"email": email, "password": PASSWORD})
    return account


def sellable(db: Session, seller: Seller, **overrides: Any) -> Product:
    """A published, priced, in-stock product."""
    values: dict[str, Any] = {
        "price_kes": 1500,
        "stock": 5,
        "status": ProductStatus.PUBLISHED.value,
    }
    values.update(overrides)
    return make_product(db, seller, **values)


def open_for_business(db: Session, seller: Seller) -> Seller:
    """
    Make a shop able to take orders: contactable, published, payable.

    The WhatsApp number is not decoration. ``published_needs_whatsapp``
    refuses to publish a shop without one, because a live storefront nobody
    can contact is worse than no storefront — and ``create_account`` does not
    ask for a number at signup.
    """
    seller.whatsapp_number = "254712345678"
    seller.is_published = True
    db.flush()
    make_payment_method(db, seller)
    return seller


def other_shop(db: Session) -> Seller:
    """A second published shop, for scoping tests."""
    other = make_seller(db, slug="other", display_name="Other")
    other.is_published = True
    db.flush()
    make_payment_method(db, other)
    return other


def an_order(db: Session, seller: Seller, **overrides: Any) -> Any:
    """A placed order for this seller, with one line."""
    product = sellable(db, seller, **overrides)
    cart = get_or_create_cart(db, None, seller)
    add_item(db, cart, product.id)
    return place_order(db, cart, buyer_name="Amina", buyer_phone="254712000111")


class TestTheLoginWall:
    def test_the_orders_page_is_behind_it(self, client: TestClient, db: Session) -> None:
        """A page listing buyers' phone numbers and addresses, above all."""
        response = client.get("/orders", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]

    def test_confirming_is_behind_it(self, client: TestClient, db: Session) -> None:
        response = client.post("/orders/BM-ABCDEFGH/confirm", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]


class TestScoping:
    def test_another_sellers_order_cannot_be_read(self, client: TestClient, db: Session) -> None:
        """
        References travel in WhatsApp messages, so they are not secrets between
        sellers. Scoping is the only thing stopping one from reading another's.
        """
        signed_in(client, db)

        stranger_order = an_order(db, other_shop(db))

        assert client.get(f"/orders/{stranger_order.reference}").status_code == 404

    def test_another_sellers_order_cannot_be_confirmed(
        self, client: TestClient, db: Session
    ) -> None:
        """The money action, from the wrong hands."""
        signed_in(client, db)

        stranger_order = an_order(db, other_shop(db))
        claim_payment(db, stranger_order, "SLK7XA2B9C")

        response = client.post(
            f"/orders/{stranger_order.reference}/confirm",
            data={"receipt": "SLK7XA2B9C"},
            follow_redirects=False,
        )

        assert response.status_code == 404
        db.refresh(stranger_order)
        assert stranger_order.status == OrderStatus.AWAITING_CONFIRMATION.value

    def test_an_unknown_reference_is_the_same_404(self, client: TestClient, db: Session) -> None:
        """Indistinguishable from someone else's, so neither is confirmed to exist."""
        signed_in(client, db)
        assert client.get("/orders/BM-NOTREAL9").status_code == 404


class TestConfirming:
    def test_a_seller_can_confirm_a_claim(self, client: TestClient, db: Session) -> None:
        """The whole point of the screen: a human turns a claim into a payment."""
        account = signed_in(client, db)
        seller = account.seller
        assert seller is not None
        open_for_business(db, seller)

        order = an_order(db, seller)
        claim_payment(db, order, "SLK7XA2B9C")

        response = client.post(
            f"/orders/{order.reference}/confirm", data={"receipt": ""}, follow_redirects=False
        )

        assert response.status_code == 303
        db.refresh(order)
        assert order.status == OrderStatus.PAID.value
        assert order.paid_at is not None
        assert order.payments[-1].mpesa_receipt == "SLK7XA2B9C"

    def test_a_corrected_receipt_replaces_the_claim(self, client: TestClient, db: Session) -> None:
        """For when the buyer mistyped and the seller reads the real one off their phone."""
        account = signed_in(client, db)
        seller = account.seller
        assert seller is not None
        open_for_business(db, seller)

        order = an_order(db, seller)
        claim_payment(db, order, "WRONGCODE")

        client.post(f"/orders/{order.reference}/confirm", data={"receipt": "slk7xa2b9c"})

        db.refresh(order)
        assert order.payments[-1].mpesa_receipt == "SLK7XA2B9C"
        assert order.payments[-1].claimed_code == "WRONGCODE"

    def test_confirming_is_not_reachable_by_get(self, client: TestClient, db: Session) -> None:
        """A money action behind a link is one crawler away from firing itself."""
        account = signed_in(client, db)
        seller = account.seller
        assert seller is not None
        open_for_business(db, seller)
        order = an_order(db, seller)

        assert client.get(f"/orders/{order.reference}/confirm").status_code == 405


class TestTheOrdersPage:
    def test_an_empty_state_when_nothing_has_sold(self, client: TestClient, db: Session) -> None:
        signed_in(client, db)
        body = client.get("/orders").text
        assert "No orders yet" in body

    def test_orders_awaiting_confirmation_are_called_out(
        self, client: TestClient, db: Session
    ) -> None:
        """
        That count is the seller's actual to-do list — a buyer has said they
        paid and is waiting to be believed.
        """
        account = signed_in(client, db)
        seller = account.seller
        assert seller is not None
        open_for_business(db, seller)

        order = an_order(db, seller)
        claim_payment(db, order, "SLK7XA2B9C")

        body = client.get("/orders").text
        assert "waiting to be confirmed" in body
        assert order.reference in body

    def test_cancelling_returns_the_stock(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)
        seller = account.seller
        assert seller is not None
        open_for_business(db, seller)

        order = an_order(db, seller)
        product = order.items[0].product
        assert product is not None
        reserved = product.stock

        client.post(f"/orders/{order.reference}/cancel")

        db.refresh(order)
        db.refresh(product)
        assert order.status == OrderStatus.CANCELLED.value
        assert product.stock == reserved + 1
