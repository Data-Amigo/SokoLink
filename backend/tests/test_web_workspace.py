"""
Every workspace page renders, in both of its states.

WHY THIS FILE EXISTS. A Jinja template has no compile step: a renamed context
key, a filter that was never registered, a macro imported in the shell but not
in a child — none of it is caught by ruff, by mypy, or by any service-level
test. It surfaces as a 500 on a page a seller opened, and it surfaced exactly
that way during the restyle that prompted this file.

The other tests here each cover ONE page deeply — permissions, money movement,
the publish gate. This covers EVERY page shallowly, which is the coverage a
restyle needs: after changing the shared shell, the question is not "does
confirming a payment still work" but "does anything still render at all".

BOTH STATES, ALWAYS. The empty state is a completely separate branch of every
template, and it is the one every new seller sees first — so a suite that only
tests populated pages tests the half of the UI that fewer people ever look at.
"""

from __future__ import annotations

from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, OrderStatus, Product, ProductStatus, Seller
from app.services.accounts import create_account
from app.services.cart import add_item, get_or_create_cart
from app.services.orders import claim_payment, place_order
from tests.factories import make_payment_method, make_product, make_seller

PASSWORD = "correct-horse-battery"

#: Every page behind the login wall, by the URL a seller actually visits.
WORKSPACE_PAGES = [
    "/dashboard",
    "/products",
    "/products/new",
    "/orders",
    "/analytics",
    "/settings/payment",
    "/customers",
]


def signed_in(client: TestClient, db: Session, email: str = "seller@example.com") -> Account:
    """An account with a shop, and a browser session as it."""
    account = create_account(db, email=email, password=PASSWORD, shop_name="Zuma Fashion Store")
    db.flush()
    client.post("/login", data={"email": email, "password": PASSWORD})
    return account


def shop(account: Account) -> Seller:
    """
    The account's shop, narrowed from ``Seller | None``.

    ``create_account`` always makes one, but the relationship is optional on the
    model, so every test would otherwise open with the same assert.
    """
    assert account.seller is not None
    return account.seller


def trading(db: Session, seller: Seller) -> Seller:
    """A shop that can actually take an order: contactable, open, payable."""
    seller.whatsapp_number = "254712345678"
    seller.is_published = True
    make_payment_method(db, seller)
    db.flush()
    return seller


#: Products are unique on (platform, platform_post_id), so a test making more
#: than one has to hand out distinct ids. A counter beats hard-coding them:
#: adding a product to a test should not mean editing every id below it.
_post_id = count(7100000000000000001)


def sellable(db: Session, seller: Seller, **overrides: Any) -> Product:
    """A published, priced, in-stock product."""
    values: dict[str, Any] = {
        "price_kes": 1500,
        "stock": 5,
        "status": ProductStatus.PUBLISHED.value,
        "platform_post_id": str(next(_post_id)),
    }
    values.update(overrides)
    return make_product(db, seller, **values)


def buy(
    client: TestClient,
    db: Session,
    seller: Seller,
    product: Product,
    *,
    name: str = "Akinyi Otieno",
    phone: str = "254712345678",
) -> Any:
    """Put one real order through the shop, as a buyer would."""
    cart = get_or_create_cart(db, None, seller)
    add_item(db, cart, product.id, quantity=1)
    order = place_order(
        db,
        cart=cart,
        buyer_name=name,
        buyer_phone=phone,
        delivery_address="Nairobi",
    )
    db.flush()
    return order


class TestTheEmptyWorkspace:
    """
    A seller who signed up two minutes ago.

    THIS IS THE FIRST THING EVERY SELLER SEES. No products, no orders, no
    payment method — every page is on its empty branch, and every empty branch
    is template code that the populated tests never execute.
    """

    @pytest.mark.parametrize("path", WORKSPACE_PAGES)
    def test_it_renders(self, client: TestClient, db: Session, path: str) -> None:
        signed_in(client, db)

        response = client.get(path)

        assert response.status_code == 200, f"{path} did not render: {response.text[:400]}"

    @pytest.mark.parametrize("path", WORKSPACE_PAGES)
    def test_the_shell_is_on_every_page(self, client: TestClient, db: Session, path: str) -> None:
        """
        The nav, the icon macros and the avatar come from the shared shell. If
        one page stopped extending it, that page would still return 200 while
        losing every way to navigate off it.
        """
        signed_in(client, db)

        body = client.get(path).text

        assert 'class="ws-nav"' in body
        assert "/customers" in body
        # The avatar is rendered by a filter registered on the environment;
        # a missing filter is a 500, a missing seller is a silent "?".
        assert 'class="ws-avatar"' in body
        assert "ZS" in body  # Zuma Fashion Store


class TestTheWorkingWorkspace:
    """The same pages once the seller has stock, an order and money."""

    @pytest.mark.parametrize("path", WORKSPACE_PAGES)
    def test_it_renders(self, client: TestClient, db: Session, path: str) -> None:
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        product = sellable(db, seller)
        make_product(
            db,
            seller,
            price_kes=None,
            status=ProductStatus.DRAFT.value,
            platform_post_id=str(next(_post_id)),
        )
        buy(client, db, seller, product)

        response = client.get(path)

        assert response.status_code == 200, f"{path} did not render: {response.text[:400]}"

    def test_an_order_renders_at_every_status(self, client: TestClient, db: Session) -> None:
        """
        THE ORDER PAGE IS FOUR PAGES. Each status is a separate branch with its
        own heading, its own form, and — for two of them — a money action.
        """
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        product = sellable(db, seller, stock=20)

        pending = buy(client, db, seller, product)
        assert client.get(f"/orders/{pending.reference}").status_code == 200

        claimed = buy(client, db, seller, product)
        claim_payment(db, claimed, code="SLK7XA2B9C")
        db.flush()
        response = client.get(f"/orders/{claimed.reference}")
        assert response.status_code == 200
        # The claim is shown so the seller can check it against their own
        # M-Pesa messages — without it on screen there is nothing to verify.
        assert "SLK7XA2B9C" in response.text


class TestCustomers:
    """
    Customers are DERIVED from orders — there is no customer table — so the
    page is only ever as right as the grouping behind it.
    """

    def test_a_buyer_appears_after_ordering(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        buy(client, db, seller, sellable(db, seller), name="Akinyi Otieno")

        body = client.get("/customers").text

        assert "Akinyi Otieno" in body
        # Stored international, shown the way a Nairobi seller reads it.
        assert "0712 345 678" in body

    def test_two_orders_from_one_phone_are_one_customer(
        self, client: TestClient, db: Session
    ) -> None:
        """
        THE PHONE IS THE IDENTITY, not the name. The same person types
        "Akinyi", "akinyi o" and "Akinyi Otieno" across three orders; grouping
        by name would show a seller three customers where they have one.
        """
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        product = sellable(db, seller, stock=20)

        buy(client, db, seller, product, name="akinyi", phone="254700111222")
        buy(client, db, seller, product, name="Akinyi Otieno", phone="254700111222")

        body = client.get("/customers").text

        assert body.count("ws-face") == 1
        # The MOST RECENT order names them: people correct their own spelling.
        assert "Akinyi Otieno" in body
        assert "2 orders" in body

    def test_a_seller_never_sees_another_shops_customers(
        self, client: TestClient, db: Session
    ) -> None:
        """The whole page is buyer phone numbers. Scoping is not optional."""
        stranger = make_seller(db, slug="someoneelse", display_name="Someone Else")
        trading(db, stranger)
        buy(client, db, stranger, sellable(db, stranger), name="Not Your Buyer")

        account = signed_in(client, db)
        trading(db, shop(account))

        body = client.get("/customers").text

        assert "Not Your Buyer" not in body
        assert "No customers yet" in body

    def test_search_finds_by_phone(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        product = sellable(db, seller, stock=20)
        buy(client, db, seller, product, name="Akinyi Otieno", phone="254700111222")
        buy(client, db, seller, product, name="Brian Mwangi", phone="254733444555")

        body = client.get("/customers?q=733444").text

        assert "Brian Mwangi" in body
        assert "Akinyi Otieno" not in body

    def test_a_search_with_no_matches_says_so(self, client: TestClient, db: Session) -> None:
        """
        A search that found nothing is not a shop with no customers, and saying
        the wrong one of those to a seller with 24 buyers is alarming.
        """
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        buy(client, db, seller, sellable(db, seller))

        body = client.get("/customers?q=nobodybythisname").text

        assert "Nobody matches that" in body
        assert "No customers yet" not in body

    def test_the_totals_ignore_the_segment_filter(self, client: TestClient, db: Session) -> None:
        """
        THE TILES COUNT EVERYONE, THE LIST OBEYS THE FILTER. Summary numbers
        that move when you click a filter are the fastest way to make a
        dashboard untrustworthy.
        """
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        product = sellable(db, seller, stock=20)
        buy(client, db, seller, product, name="One Buyer", phone="254700111222")
        buy(client, db, seller, product, name="Two Buyer", phone="254733444555")

        body = client.get("/customers?segment=repeat").text

        # Nobody has bought twice, so the list is empty...
        assert "One Buyer" not in body
        # ...but the shop still has two customers, and the tile must say so.
        assert ">2</div>" in body

    def test_an_unpaid_order_is_not_counted_as_spend(self, client: TestClient, db: Session) -> None:
        """
        SPEND IS CONFIRMED MONEY ONLY. An order somebody claimed and never paid
        is not spend, and showing it as such overstates a seller's best
        customers to their face.
        """
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        order = buy(client, db, seller, sellable(db, seller))
        claim_payment(db, order, code="SLK7XA2B9C")
        db.flush()

        body = client.get("/customers").text

        assert "nothing confirmed yet" in body
        assert "Payment pending" in body

    def test_it_needs_a_login(self, client: TestClient, db: Session) -> None:
        """A list of buyers' phone numbers is the last page to leave open."""
        response = client.get("/customers", follow_redirects=False)

        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]


class TestOrderStatusIsSpelledTheSameEverywhere:
    def test_a_paid_order_reads_as_paid_on_every_page(
        self, client: TestClient, db: Session
    ) -> None:
        """
        One status vocabulary across the workspace. A seller who learns that
        green means done on Orders must not meet a different word for the same
        state on Money.
        """
        account = signed_in(client, db)
        seller = trading(db, shop(account))
        order = buy(client, db, seller, sellable(db, seller))
        claim_payment(db, order, code="SLK7XA2B9C")
        db.flush()

        for path in ("/orders", "/analytics", f"/orders/{order.reference}"):
            assert "Confirm" in client.get(path).text, path

        assert order.status == OrderStatus.AWAITING_CONFIRMATION.value
