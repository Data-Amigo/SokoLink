"""
The publish screen — the HTTP shell around the gate.

``test_catalogue.py`` covers the service: what may be published and by whom.
This covers what goes wrong at the route layer:

  **Product ids are sequential integers in a URL.** Three routes take one. All
  three must be scoped to the signed-in seller, or a guessed number lets anyone
  publish — or unpublish — a stranger's stock.

  **Publishing is a state change.** It must be POST-only. A money-adjacent
  action behind a link is one crawler away from firing itself.

  **The gate must be visible before it is hit.** An unpriced product shows a
  disabled button, not an error after the click.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, Product, ProductStatus, Seller
from app.services.accounts import create_account
from app.services.catalogue import publish_product
from tests.factories import make_product, make_seller

PASSWORD = "correct-horse-battery"


def signed_in(client: TestClient, db: Session, email: str = "seller@example.com") -> Account:
    account = create_account(db, email=email, password=PASSWORD, shop_name="Nairobi Thrift")
    db.flush()
    client.post("/login", data={"email": email, "password": PASSWORD})
    return account


def contactable(db: Session, account: Account) -> Seller:
    """A seller who can legally be published — i.e. has a WhatsApp number."""
    seller = account.seller
    assert seller is not None
    seller.whatsapp_number = "254712345678"
    db.flush()
    return seller


def draft(db: Session, seller: Seller, **overrides: Any) -> Product:
    values: dict[str, Any] = {"price_kes": 1500, "stock": 3}
    values.update(overrides)
    return make_product(db, seller, **values)


class TestTheLoginWall:
    def test_the_products_page_is_behind_it(self, client: TestClient, db: Session) -> None:
        response = client.get("/products", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]

    def test_publishing_is_behind_it(self, client: TestClient, db: Session) -> None:
        response = client.post("/products/1/publish", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]


class TestPublishing:
    def test_a_seller_can_publish_their_own_product(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)
        seller = contactable(db, account)
        product = draft(db, seller)

        response = client.post(f"/products/{product.id}/publish", follow_redirects=False)

        assert response.status_code == 303
        db.refresh(product)
        assert product.status == ProductStatus.PUBLISHED.value

    def test_publishing_without_a_price_returns_a_readable_reason(
        self, client: TestClient, db: Session
    ) -> None:
        """Not a 500, and not a silent no-op — a sentence naming the item."""
        account = signed_in(client, db)
        seller = contactable(db, account)
        product = draft(db, seller, price_kes=None, title="Leather Bag")

        response = client.post(f"/products/{product.id}/publish", follow_redirects=False)

        assert response.status_code == 303
        assert (
            "Leather%20Bag" in response.headers["location"]
            or "Leather Bag" in (response.headers["location"])
        )
        db.refresh(product)
        assert product.status == ProductStatus.DRAFT.value

    def test_publishing_is_not_reachable_by_get(self, client: TestClient, db: Session) -> None:
        """A state change behind a link is one crawler away from firing itself."""
        account = signed_in(client, db)
        seller = contactable(db, account)
        product = draft(db, seller)

        assert client.get(f"/products/{product.id}/publish").status_code == 405

    def test_unpublishing_takes_it_off_the_storefront(
        self, client: TestClient, db: Session
    ) -> None:
        account = signed_in(client, db)
        seller = contactable(db, account)
        product = draft(db, seller)
        publish_product(db, seller, product.id)

        client.post(f"/products/{product.id}/unpublish")

        db.refresh(product)
        assert product.status == ProductStatus.DRAFT.value


class TestScoping:
    def test_another_sellers_product_cannot_be_published(
        self, client: TestClient, db: Session
    ) -> None:
        """The one that matters: ids are sequential and guessable."""
        signed_in(client, db)
        other = make_seller(db, slug="other", display_name="Other")
        stranger = draft(db, other)

        client.post(f"/products/{stranger.id}/publish")

        db.refresh(stranger)
        assert stranger.status == ProductStatus.DRAFT.value

    def test_another_sellers_product_cannot_be_unpublished(
        self, client: TestClient, db: Session
    ) -> None:
        """The more damaging direction: taking a competitor's stock offline."""
        signed_in(client, db)
        other = make_seller(db, slug="other", display_name="Other")
        stranger = draft(db, other)
        publish_product(db, other, stranger.id)

        client.post(f"/products/{stranger.id}/unpublish")

        db.refresh(stranger)
        assert stranger.status == ProductStatus.PUBLISHED.value


class TestOpeningTheShop:
    def test_a_shop_without_a_whatsapp_number_is_refused(
        self, client: TestClient, db: Session
    ) -> None:
        """``create_account`` does not ask for a number, so this is the common case."""
        account = signed_in(client, db)
        seller = account.seller
        assert seller is not None

        response = client.post("/settings/shop/open", follow_redirects=False)

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        db.refresh(seller)
        assert seller.is_published is False

    def test_opening_makes_the_storefront_reachable(self, client: TestClient, db: Session) -> None:
        """End to end: the button opens a shop a buyer can actually load."""
        account = signed_in(client, db)
        seller = contactable(db, account)
        product = draft(db, seller, title="Cargo Pants")

        client.post("/settings/shop/open")
        client.post(f"/products/{product.id}/publish")

        storefront = client.get(f"/shop/{seller.slug}")
        assert storefront.status_code == 200
        assert "Cargo Pants" in storefront.text

    def test_closing_hides_it_again(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)
        seller = contactable(db, account)
        client.post("/settings/shop/open")

        client.post("/settings/shop/close")

        assert client.get(f"/shop/{seller.slug}").status_code == 404


class TestThePage:
    def test_it_warns_when_payments_are_not_set_up(self, client: TestClient, db: Session) -> None:
        """Publishing into a shop that cannot take money is wasted work."""
        signed_in(client, db)
        assert "cannot take orders yet" in client.get("/products").text

    def test_an_unpriced_product_shows_a_disabled_button(
        self, client: TestClient, db: Session
    ) -> None:
        """The gate is visible before it is hit, not after."""
        account = signed_in(client, db)
        seller = contactable(db, account)
        draft(db, seller, price_kes=None, title="No Price Yet")

        body = client.get("/products").text
        assert "Needs price" in body
        assert "disabled" in body

    def test_the_empty_state_says_where_products_come_from(
        self, client: TestClient, db: Session
    ) -> None:
        signed_in(client, db)
        assert "Nothing here yet" in client.get("/products").text


class TestAddingByHand:
    def test_the_form_is_behind_the_login_wall(self, client: TestClient, db: Session) -> None:
        response = client.get("/products/new", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]

    def test_a_name_and_price_is_enough(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)
        contactable(db, account)

        response = client.post(
            "/products/new",
            data={"title": "Mixed Ladies Sandals", "price_kes": "3000", "stock": "5"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "/products?added=" in response.headers["location"]
        assert "Mixed Ladies Sandals" in client.get("/products").text

    def test_a_price_with_a_comma_is_understood(self, client: TestClient, db: Session) -> None:
        """People type "3,000". Rejecting that would be pedantry, not validation."""
        account = signed_in(client, db)
        contactable(db, account)

        client.post("/products/new", data={"title": "Bale", "price_kes": "3,000"})

        assert "KES 3,000" in client.get("/products").text

    def test_a_non_numeric_price_gets_a_readable_error(
        self, client: TestClient, db: Session
    ) -> None:
        """Not a 422 the seller cannot read."""
        account = signed_in(client, db)
        contactable(db, account)

        response = client.post(
            "/products/new",
            data={"title": "Thing", "price_kes": "about three thousand"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]

    def test_a_hand_typed_product_reaches_the_storefront(
        self, client: TestClient, db: Session
    ) -> None:
        """
        THE WHOLE POINT. Type it, publish it, open the shop, and a buyer can
        load it — with no scrape, no AI and no WhatsApp involved.
        """
        account = signed_in(client, db)
        seller = contactable(db, account)

        client.post("/products/new", data={"title": "Cargo Pants", "price_kes": "1500"})
        product = db.query(Product).filter(Product.title == "Cargo Pants").one()

        client.post(f"/products/{product.id}/publish")
        client.post("/settings/shop/open")

        storefront = client.get(f"/shop/{seller.slug}")
        assert storefront.status_code == 200
        assert "Cargo Pants" in storefront.text
        assert "KES 1,500" in storefront.text
