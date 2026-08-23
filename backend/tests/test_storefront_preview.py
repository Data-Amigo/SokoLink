"""
A seller previewing their own closed shop.

WHY THIS EXISTS. "View store" in the workspace pointed at the public storefront,
which 404s while a shop is closed — so the button was broken for every seller
who had not yet opened, which is every new seller. Previewing is most needed
exactly when the public route refuses.

THE SECURITY QUESTION IS THE WHOLE FILE. Widening who may see a closed shop is
one careless check away from publishing every seller's half-parsed drafts, so
the tests below spend most of their effort proving what preview does NOT do:

    a stranger              still gets 404
    another seller          still gets 404
    drafts                  still hidden, even from the owner
    checkout                still refuses, even for the owner

The owner sees what a BUYER would see, on a page nobody else can load. That is
the entire feature.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Account, Product, ProductStatus, Seller
from app.services.accounts import create_account
from tests.factories import make_payment_method, make_product

PASSWORD = "correct-horse-battery"


def signed_in(
    client: TestClient,
    db: Session,
    email: str = "seller@example.com",
    shop_name: str = "Zuma Fashion Store",
) -> Account:
    """An account with a shop, and a browser session as it."""
    account = create_account(db, email=email, password=PASSWORD, shop_name=shop_name)
    db.flush()
    client.post("/login", data={"email": email, "password": PASSWORD})
    return account


def shop(account: Account) -> Seller:
    """The account's shop, narrowed from ``Seller | None``."""
    assert account.seller is not None
    return account.seller


def stocked(db: Session, seller: Seller, **overrides: Any) -> Product:
    """One published, priced product, so the shop has something to show."""
    values: dict[str, Any] = {
        "title": "Ankara Shirt",
        "price_kes": 1500,
        "stock": 5,
        "status": ProductStatus.PUBLISHED.value,
        "platform_post_id": f"71000000000000{seller.id:04d}",
    }
    values.update(overrides)
    return make_product(db, seller, **values)


class TestTheOwnerCanPreview:
    def test_a_closed_shop_opens_for_its_owner(self, client: TestClient, db: Session) -> None:
        """
        THE BUG THIS FIXES. Every new seller's shop is closed, so "View store"
        returned "Shop not found" to everyone who had not yet opened — which is
        precisely the group who most need to look before they do.
        """
        account = signed_in(client, db)
        seller = shop(account)
        stocked(db, seller)
        assert seller.is_published is False

        response = client.get(f"/shop/{seller.slug}")

        assert response.status_code == 200
        assert seller.display_name in response.text

    def test_the_preview_says_it_is_a_preview(self, client: TestClient, db: Session) -> None:
        """
        A seller looking at their own shop must not conclude it is live. The bar
        states both halves: nobody else can see this, and here is the button
        that changes it.
        """
        account = signed_in(client, db)
        seller = shop(account)
        stocked(db, seller)

        body = client.get(f"/shop/{seller.slug}").text

        assert "Preview" in body
        assert "buyers cannot see this yet" in body.lower()

    def test_a_product_page_previews_too(self, client: TestClient, db: Session) -> None:
        """Browsing a preview that dead-ends on the first tap is not a preview."""
        account = signed_in(client, db)
        seller = shop(account)
        product = stocked(db, seller)

        response = client.get(f"/shop/{seller.slug}/{product.id}")

        assert response.status_code == 200
        assert "Ankara Shirt" in response.text

    def test_an_open_shop_is_not_marked_as_a_preview(self, client: TestClient, db: Session) -> None:
        """The bar must vanish the moment the shop is real."""
        account = signed_in(client, db)
        seller = shop(account)
        stocked(db, seller)
        # ck_sellers_published_needs_whatsapp: a live shop nobody can contact is
        # a dead end, so Postgres refuses to open one without a number.
        seller.whatsapp_number = "254712345678"
        seller.is_published = True
        db.flush()

        body = client.get(f"/shop/{seller.slug}").text

        assert "buyers cannot see this yet" not in body.lower()


class TestPreviewIsNotAHoleInThePublishGate:
    """
    Everything preview must NOT do. This is the half that matters: the feature
    widens who may LOOK at a closed shop, and nothing else.
    """

    def test_a_stranger_still_gets_a_404(self, client: TestClient, db: Session) -> None:
        account = signed_in(client, db)
        seller = shop(account)
        stocked(db, seller)
        client.post("/logout")

        response = client.get(f"/shop/{seller.slug}")

        assert response.status_code == 404

    def test_another_seller_still_gets_a_404(self, client: TestClient, db: Session) -> None:
        """
        THE ONE THAT WOULD HURT. A signed-in session is not permission to see
        somebody else's unopened shop — that is their unfinished work, and
        showing it would leak every draft they have published.
        """
        owner = signed_in(client, db, email="owner@example.com", shop_name="Zuma Fashion")
        victim = shop(owner)
        stocked(db, victim)
        client.post("/logout")

        signed_in(client, db, email="nosy@example.com", shop_name="Someone Else")
        response = client.get(f"/shop/{victim.slug}")

        assert response.status_code == 404

    def test_drafts_stay_hidden_from_the_owner(self, client: TestClient, db: Session) -> None:
        """
        A PREVIEW ANSWERS "WHAT WILL A BUYER SEE", so it shows what a buyer
        would see and nothing more. An owner who saw their drafts here would
        open the shop believing those were live.
        """
        account = signed_in(client, db)
        seller = shop(account)
        stocked(db, seller)
        make_product(
            db,
            seller,
            title="Secret Draft Bag",
            price_kes=900,
            status=ProductStatus.DRAFT.value,
            platform_post_id="7100000000000000999",
        )

        body = client.get(f"/shop/{seller.slug}").text

        assert "Ankara Shirt" in body
        assert "Secret Draft Bag" not in body

    def test_checkout_still_refuses_on_a_closed_shop(self, client: TestClient, db: Session) -> None:
        """
        Preview covers the READ pages only. A closed shop cannot take an order,
        not even from the person who owns it — otherwise "open my shop" would
        stop meaning anything.
        """
        account = signed_in(client, db)
        seller = shop(account)
        stocked(db, seller)
        make_payment_method(db, seller)
        db.flush()

        assert client.get(f"/shop/{seller.slug}/checkout").status_code == 404
        assert client.get(f"/shop/{seller.slug}/cart").status_code == 404

    def test_an_unknown_slug_is_still_a_404_for_a_signed_in_seller(
        self, client: TestClient, db: Session
    ) -> None:
        signed_in(client, db)

        assert client.get("/shop/no-such-shop").status_code == 404


class TestTheLinkPreviewCard:
    """
    The card a chat app draws around a pasted shop link.

    THIS PRODUCT'S WHOLE DISTRIBUTION IS A PASTED URL — a WhatsApp chat, a
    status, a bio. Far more people see the card than ever tap it, so the card is
    the shopfront. A grey rectangle with a domain in it reads as a broken link.
    """

    def test_a_shop_without_an_avatar_advertises_its_newest_stock(
        self, client: TestClient, db: Session
    ) -> None:
        """
        Almost no seller has an avatar yet, so without this fallback almost
        every shop link shares as a grey box.
        """
        account = signed_in(client, db)
        seller = shop(account)
        assert seller.avatar_url is None
        stocked(db, seller, cover_url="https://example.test/shirt.jpg")

        body = client.get(f"/shop/{seller.slug}").text

        assert 'property="og:image"' in body
        assert "https://example.test/shirt.jpg" in body

    def test_an_avatar_wins_when_the_seller_has_one(self, client: TestClient, db: Session) -> None:
        """Their own brand beats a photograph of one item they happen to sell."""
        account = signed_in(client, db)
        seller = shop(account)
        seller.avatar_url = "https://example.test/logo.png"
        stocked(db, seller, cover_url="https://example.test/shirt.jpg")
        db.flush()

        body = client.get(f"/shop/{seller.slug}").text

        assert "https://example.test/logo.png" in body
        assert "https://example.test/shirt.jpg" not in body.split("</head>")[0]

    def test_a_draft_cover_is_never_advertised(self, client: TestClient, db: Session) -> None:
        """
        A DRAFT IS NOT FOR SALE. Advertising one in the card promises a buyer
        something they cannot find when they tap through.
        """
        account = signed_in(client, db)
        seller = shop(account)
        make_product(
            db,
            seller,
            title="Unfinished Thing",
            status=ProductStatus.DRAFT.value,
            cover_url="https://example.test/draft.jpg",
            platform_post_id="7100000000000000777",
        )

        head = client.get(f"/shop/{seller.slug}").text.split("</head>")[0]

        assert "https://example.test/draft.jpg" not in head
