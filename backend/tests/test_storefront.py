"""
The buyer's storefront — visibility rules, the basket, and the WhatsApp handoff.

**These are the first tests this surface has ever had.** It shipped with the
visibility rules written down in prose and enforced by four queries nobody had
watched fail. Everything below asserts a rule that, if it broke, would either
leak one seller's data into another's shop or take money for something the buyer
cannot have.

The two that matter most:

    a guessed product id must not render under another seller's header
    a basket belongs to one seller, because payment goes straight to that seller
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Product, ProductStatus, Seller
from app.services.cart import (
    CART_COOKIE,
    MAX_QUANTITY_PER_LINE,
    CartError,
    add_item,
    get_cart,
    get_or_create_cart,
    set_quantity,
    unavailable_lines,
)
from app.services.storefront import (
    build_shop_whatsapp_url,
    build_whatsapp_url,
    get_categories,
    get_public_product,
    get_public_products,
    get_public_shop,
)
from tests.factories import make_product, make_seller


def publish(db: Session, seller_kwargs: dict[str, Any] | None = None) -> Seller:
    """A published shop — the only kind a buyer can reach."""
    seller = make_seller(db, **(seller_kwargs or {}))
    seller.is_published = True
    db.flush()
    return seller


def publish_product(db: Session, seller: Seller, **overrides: Any) -> Product:
    """A published, priced, in-stock product."""
    values: dict[str, Any] = {
        "price_kes": 1500,
        "stock": 5,
        "status": ProductStatus.PUBLISHED.value,
    }
    values.update(overrides)
    return make_product(db, seller, **values)


class TestWhatABuyerMaySee:
    def test_an_unpublished_shop_is_invisible(self, db: Session) -> None:
        """
        Unknown and unpublished must be indistinguishable.

        An unpublished shop is full of half-parsed AI drafts. Rendering it would
        embarrass the seller; returning a different status would confirm the
        slug exists to anyone probing.
        """
        make_seller(db, slug="hidden")
        assert get_public_shop(db, "hidden") is None
        assert get_public_shop(db, "does-not-exist") is None

    def test_only_published_products_are_listed(self, db: Session) -> None:
        seller = publish(db)
        publish_product(db, seller, title="Live")
        make_product(db, seller, title="Draft", platform_post_id="draft-1")

        titles = [p.title for p in get_public_products(db, seller)]
        assert titles == ["Live"]

    def test_sold_out_items_are_shown_but_sort_last(self, db: Session) -> None:
        """
        A shop that hides sold-out stock looks dead to a returning buyer.

        The badge says the shop is real and worth asking about — and in-stock
        must still lead, or the first screen advertises what cannot be bought.
        """
        seller = publish(db)
        publish_product(db, seller, title="Gone", stock=0, platform_post_id="a")
        publish_product(db, seller, title="Here", stock=3, platform_post_id="b")

        assert [p.title for p in get_public_products(db, seller)] == ["Here", "Gone"]

    def test_a_product_is_scoped_to_its_shop(self, db: Session) -> None:
        """
        THE ONE THAT MATTERS MOST.

        Without the seller in this query, a guessed id renders another seller's
        product under this seller's header — with this seller's WhatsApp number
        underneath it. That is sales diversion, invisible to the buyer.
        """
        mine = publish(db, {"slug": "mine"})
        theirs = publish(db, {"slug": "theirs", "display_name": "Theirs"})
        stolen = publish_product(db, theirs, title="Theirs Only")

        assert get_public_product(db, mine, stolen.id) is None
        assert get_public_product(db, theirs, stolen.id) is not None


class TestFilteringAndSearch:
    def test_categories_come_from_published_products_only(self, db: Session) -> None:
        """A pill leading to an empty page is worse than no pill."""
        seller = publish(db)
        publish_product(db, seller, category="Shoes", platform_post_id="a")
        make_product(db, seller, category="Secret", platform_post_id="b")

        assert get_categories(db, seller) == ["Shoes"]

    def test_search_matches_title_and_description(self, db: Session) -> None:
        seller = publish(db)
        publish_product(db, seller, title="Leather Bag", platform_post_id="a")
        publish_product(
            db, seller, title="Sandals", description="soft leather", platform_post_id="b"
        )
        publish_product(db, seller, title="Cap", platform_post_id="c")

        found = {p.title for p in get_public_products(db, seller, search="leather")}
        assert found == {"Leather Bag", "Sandals"}

    def test_a_wildcard_in_a_search_is_literal(self, db: Session) -> None:
        """
        Unescaped, "%" matches everything and the buyer gets the whole shop back
        as if it were a result. Escaping keeps a search for "50% off" honest.
        """
        seller = publish(db)
        publish_product(db, seller, title="50% off bundle", platform_post_id="a")
        publish_product(db, seller, title="Plain Shirt", platform_post_id="b")

        found = [p.title for p in get_public_products(db, seller, search="50%")]
        assert found == ["50% off bundle"]

    def test_sorting_by_price_keeps_in_stock_first(self, db: Session) -> None:
        """The cheapest thing a buyer cannot have must not lead the page."""
        seller = publish(db)
        publish_product(
            db, seller, title="Cheap Gone", price_kes=100, stock=0, platform_post_id="a"
        )
        publish_product(db, seller, title="Dear Here", price_kes=900, stock=2, platform_post_id="b")

        ordered = [p.title for p in get_public_products(db, seller, sort="price_low")]
        assert ordered == ["Dear Here", "Cheap Gone"]


class TestTheWhatsAppHandoff:
    def test_the_message_carries_the_price_as_displayed(self, db: Session) -> None:
        """
        "KES 3,000" and "KES 3,000 for 30 pairs" mean different things to
        someone deciding whether to send money. If the page and the message
        disagree, the conversation starts with a dispute.
        """
        seller = publish(db)
        product = publish_product(
            db, seller, title="Sandals", price_kes=3000, unit_quantity=30, unit_label="pairs"
        )

        url = build_whatsapp_url(seller, product)
        assert url is not None
        assert "for%2030%20pairs" in url

    def test_no_number_means_no_button(self, db: Session) -> None:
        """A button that goes nowhere is worse than no button."""
        seller = make_seller(db, whatsapp_number=None)
        product = publish_product(db, seller)

        assert build_whatsapp_url(seller, product) is None
        assert build_shop_whatsapp_url(seller) is None


class TestTheBasket:
    def test_a_cart_is_scoped_to_one_seller(self, db: Session) -> None:
        """
        Money goes straight to the seller, so a basket spanning two shops would
        be two payments behind one Checkout button. Presenting the same token at
        another shop yields a fresh basket, not the first one.
        """
        mine = publish(db, {"slug": "mine"})
        theirs = publish(db, {"slug": "theirs", "display_name": "Theirs"})

        cart = get_or_create_cart(db, None, mine)
        assert get_cart(db, cart.token, theirs) is None

    def test_another_sellers_product_cannot_be_added(self, db: Session) -> None:
        mine = publish(db, {"slug": "mine"})
        theirs = publish(db, {"slug": "theirs", "display_name": "Theirs"})
        stolen = publish_product(db, theirs)

        cart = get_or_create_cart(db, None, mine)
        with pytest.raises(CartError):
            add_item(db, cart, stolen.id)

    def test_an_unpublished_product_cannot_be_added(self, db: Session) -> None:
        seller = publish(db)
        draft = make_product(db, seller, price_kes=500)

        cart = get_or_create_cart(db, None, seller)
        with pytest.raises(CartError):
            add_item(db, cart, draft.id)

    def test_adding_the_same_line_twice_increments_it(self, db: Session) -> None:
        """
        Two rows for one shoe makes a buyer distrust the total more than the
        duplicate itself ever cost.
        """
        seller = publish(db)
        product = publish_product(db, seller)
        cart = get_or_create_cart(db, None, seller)

        add_item(db, cart, product.id, selected_variant="40")
        add_item(db, cart, product.id, selected_variant="40")

        db.refresh(cart)
        assert len(cart.items) == 1
        assert cart.items[0].quantity == 2

    def test_different_variants_are_different_lines(self, db: Session) -> None:
        seller = publish(db)
        product = publish_product(db, seller)
        cart = get_or_create_cart(db, None, seller)

        add_item(db, cart, product.id, selected_variant="40")
        add_item(db, cart, product.id, selected_variant="42")

        db.refresh(cart)
        assert len(cart.items) == 2

    def test_a_no_variant_product_still_de_duplicates(self, db: Session) -> None:
        """
        The regression this guards: with a NULLABLE variant column, Postgres
        treats NULLs as distinct in a unique index, so tapping Add twice made
        two rows. The column is NOT NULL DEFAULT '' for exactly this reason.
        """
        seller = publish(db)
        product = publish_product(db, seller)
        cart = get_or_create_cart(db, None, seller)

        add_item(db, cart, product.id)
        add_item(db, cart, product.id)

        db.refresh(cart)
        assert len(cart.items) == 1
        assert cart.items[0].quantity == 2

    def test_quantity_is_capped(self, db: Session) -> None:
        """A basket asking for 900 is a fat finger or a bot; either way the
        seller does not have 900."""
        seller = publish(db)
        product = publish_product(db, seller)
        cart = get_or_create_cart(db, None, seller)

        with pytest.raises(CartError):
            add_item(db, cart, product.id, quantity=MAX_QUANTITY_PER_LINE + 1)

    def test_setting_quantity_to_zero_removes_the_line(self, db: Session) -> None:
        seller = publish(db)
        product = publish_product(db, seller)
        cart = get_or_create_cart(db, None, seller)
        item = add_item(db, cart, product.id)

        set_quantity(db, cart, item.id, 0)
        db.refresh(cart)
        assert cart.items == []

    def test_a_line_in_another_basket_cannot_be_edited(self, db: Session) -> None:
        """Line ids are sequential, so scoping to the cart is the only guard."""
        seller = publish(db)
        product = publish_product(db, seller)

        victim = get_or_create_cart(db, None, seller)
        item = add_item(db, victim, product.id)
        attacker = get_or_create_cart(db, None, seller)

        with pytest.raises(CartError):
            set_quantity(db, attacker, item.id, 99)

    def test_items_that_went_away_are_flagged(self, db: Session) -> None:
        """
        A basket can sit for days. Finding out an item is gone AFTER an M-Pesa
        prompt means the buyer was asked for money under false pretences.
        """
        seller = publish(db)
        product = publish_product(db, seller)
        cart = get_or_create_cart(db, None, seller)
        add_item(db, cart, product.id)

        assert unavailable_lines(cart) == []

        product.stock = 0
        db.flush()
        db.refresh(cart)
        assert len(unavailable_lines(cart)) == 1


class TestTheRoutes:
    def test_an_unpublished_shop_is_a_404(self, client: TestClient, db: Session) -> None:
        make_seller(db, slug="hidden")
        assert client.get("/shop/hidden").status_code == 404

    def test_the_shop_page_renders(self, client: TestClient, db: Session) -> None:
        seller = publish(db)
        publish_product(db, seller, title="Cargo Pants")

        response = client.get(f"/shop/{seller.slug}")
        assert response.status_code == 200
        assert "Cargo Pants" in response.text

    def test_the_storefront_no_longer_owns_the_root(self, client: TestClient, db: Session) -> None:
        """
        The reason the `/shop` prefix exists. At the root, `/{slug}` matched
        everything, so health checks depended on router registration order and
        on a reserved-word list nobody remembered to update.
        """
        publish(db, {"slug": "health"})
        assert client.get("/health").status_code == 200

    def test_adding_to_the_basket_sets_a_cookie_and_redirects(
        self, client: TestClient, db: Session
    ) -> None:
        seller = publish(db)
        product = publish_product(db, seller)

        response = client.post(
            f"/shop/{seller.slug}/cart/add",
            data={"product_id": str(product.id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/shop/{seller.slug}/cart"
        assert CART_COOKIE in response.cookies

    def test_the_og_image_is_absolute(self, client: TestClient, db: Session) -> None:
        """
        A relative og:image gives a WhatsApp link preview with no picture —
        the first impression, wasted, on the surface the product depends on.
        """
        seller = publish(db)
        product = publish_product(db, seller, cover_url="covers/abc.jpg")

        response = client.get(f"/shop/{seller.slug}/{product.id}")
        assert 'property="og:image" content="http' in response.text
