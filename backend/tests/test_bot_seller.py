"""
The seller's side of the thread: forward, price, publish, open.

This closes the loop the product is named for. Before it, the bot asked a
seller for a price and had no state in which to hear the answer — the whole
conversation was buyer states, so a number typed by a seller was read as a menu
choice and bounced them to a shop menu.

THE RULE THIS FILE EXISTS TO PROTECT. The model reports only a price it could
literally see; the human supplies every other price. Those are the two halves of
keeping a wrong number off a buyer's screen, and the second half is what lives
here. A test that let the model's guess become the price would quietly delete
the point of the first half.

PUBLISHING STAYS A GATE. It goes through the same ``publish_product`` the
workspace calls, so the chat cannot become a second way to go live that the
review queue never sees.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ConversationState, PriceSource, Product, ProductStatus, Seller
from app.schemas.draft import ProductDraft
from app.services import intake
from app.services.bot import get_conversation, handle
from tests.factories import make_product, make_seller

SELLER_PHONE = "254712345678"


def a_draft(name: str, **overrides: Any) -> ProductDraft:
    """What the model proposes: a name, and no price it could not see."""
    values: dict[str, Any] = {
        "is_product": True,
        "name": name,
        "description": "Forwarded stock.",
        "price_kes": None,
        "unit_quantity": None,
        "unit_label": None,
        "price_evidence": None,
        "confidence": 0.3,
    }
    values.update(overrides)
    return ProductDraft.model_validate(values)


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    A model that names each forwarded photo in turn and never sees a price.

    Returns the list of names it will produce, so a test can extend it.
    """
    names = ["Ankara Shirt", "Beaded Belt", "Canvas Tote"]
    pending = iter(names)

    class Fake:
        @staticmethod
        def draft_from_forwarded(caption: str, image: bytes) -> ProductDraft:
            return a_draft(next(pending))

    monkeypatch.setattr(intake, "get_draft_agent", lambda: Fake())
    return names


def seller_with_number(db: Session, **overrides: Any) -> Seller:
    """A shop whose WhatsApp number is the one the tests message from."""
    return make_seller(db, whatsapp_number=SELLER_PHONE, **overrides)


def say(db: Session, text: str, media: int = 0) -> str:
    """One message from the seller; returns everything the bot said."""
    attachments = [(f"m{i}", (lambda: b"jpeg-bytes")) for i in range(media)]
    outcome = handle(db, SELLER_PHONE, text, media=attachments or None)
    return "\n".join(r.body for r in outcome.replies)


class TestForwardingAsksForThePrice:
    def test_one_photo_asks_by_name(self, db: Session, agent: list[str]) -> None:
        """
        NAMING THE ITEM IS WHAT MAKES A BARE NUMBER SAFE. Without it, "1800"
        could answer any of eight questions and we would be guessing which
        product a seller just priced.
        """
        seller_with_number(db)

        said = say(db, "New stock", media=1)

        assert "Ankara Shirt" in said
        assert "What's the price" in said

    def test_the_conversation_enters_the_pricing_state(self, db: Session, agent: list[str]) -> None:
        seller_with_number(db)

        say(db, "New stock", media=1)

        convo = get_conversation(db, SELLER_PHONE)
        assert convo.state == ConversationState.PRICING

    def test_several_photos_are_asked_about_one_at_a_time(
        self, db: Session, agent: list[str]
    ) -> None:
        """
        A seller forwarding a burst gets one question, not three. Asking "what
        are the prices" makes them reconstruct an order we never showed them.
        """
        seller_with_number(db)

        said = say(db, "New arrivals", media=3)

        assert said.count("What's the price") == 1
        assert "(3 left)" in said

    def test_a_photo_the_model_priced_is_not_queued(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the price was printed on the image there is nothing to ask."""

        class Fake:
            @staticmethod
            def draft_from_forwarded(caption: str, image: bytes) -> ProductDraft:
                return a_draft("Ankara Shirt", price_kes=1800, confidence=0.9)

        monkeypatch.setattr(intake, "get_draft_agent", lambda: Fake())
        seller_with_number(db)

        said = say(db, "", media=1)

        assert "What's the price" not in said
        assert get_conversation(db, SELLER_PHONE).state != ConversationState.PRICING


class TestAnsweringWithAPrice:
    def test_a_number_sets_the_price(self, db: Session, agent: list[str]) -> None:
        seller = seller_with_number(db)
        say(db, "New stock", media=1)

        say(db, "1800")

        product = db.scalar(select(Product).where(Product.seller_id == seller.id))
        assert product is not None
        assert product.price_kes == 1800

    def test_the_price_is_recorded_as_the_seller_s(self, db: Session, agent: list[str]) -> None:
        """
        Provenance matters: a human price always outranks anything a model
        produced, and the cascade's own accounting depends on not confusing the
        two.
        """
        seller = seller_with_number(db)
        say(db, "New stock", media=1)

        say(db, "1800")

        product = db.scalar(select(Product).where(Product.seller_id == seller.id))
        assert product is not None
        assert product.price_source == PriceSource.SELLER.value
        assert product.price_evidence is None

    @pytest.mark.parametrize("typed", ["1800", "1,800", "KSh 1800", "1800/=", " 1800 "])
    def test_prices_are_read_the_way_a_seller_writes_them(
        self, db: Session, agent: list[str], typed: str
    ) -> None:
        """Nobody types a bare integer when they mean money."""
        seller = seller_with_number(db)
        say(db, "New stock", media=1)

        say(db, typed)

        product = db.scalar(select(Product).where(Product.seller_id == seller.id))
        assert product is not None
        assert product.price_kes == 1800

    def test_a_decimal_is_refused_rather_than_rounded(self, db: Session, agent: list[str]) -> None:
        """
        Money here is integer KES. Silently turning 1800.50 into 1800 loses
        fifty cents of somebody else's money without telling them.
        """
        seller = seller_with_number(db)
        say(db, "New stock", media=1)

        said = say(db, "1800.50")

        product = db.scalar(select(Product).where(Product.seller_id == seller.id))
        assert product is not None
        assert product.price_kes is None
        assert "just the number" in said.lower()

    def test_it_moves_on_to_the_next_item(self, db: Session, agent: list[str]) -> None:
        seller_with_number(db)
        say(db, "New arrivals", media=2)

        said = say(db, "1800")

        assert "Ankara Shirt" in said
        assert "Beaded Belt" in said

    def test_skip_leaves_one_unpriced_and_continues(self, db: Session, agent: list[str]) -> None:
        seller_with_number(db)
        say(db, "New arrivals", media=2)

        said = say(db, "skip")

        assert "Beaded Belt" in said
        shirt = db.scalar(select(Product).where(Product.title == "Ankara Shirt"))
        assert shirt is not None
        assert shirt.price_kes is None

    def test_a_seller_is_never_shown_a_shop_menu_mid_pricing(
        self, db: Session, agent: list[str]
    ) -> None:
        """
        THE BUG THIS WHOLE FILE FIXES. Before the seller branch existed, a bare
        number from a seller was read as a buyer's menu choice.
        """
        seller_with_number(db)
        say(db, "New stock", media=1)

        said = say(db, "1")

        assert "What are you looking for?" not in said


class TestPublishing:
    def _priced(self, db: Session) -> Seller:
        seller = seller_with_number(db)
        say(db, "New stock", media=1)
        say(db, "1800")
        return seller

    def test_publish_makes_the_product_live(self, db: Session, agent: list[str]) -> None:
        seller = self._priced(db)

        say(db, "publish")

        product = db.scalar(select(Product).where(Product.seller_id == seller.id))
        assert product is not None
        assert product.status == ProductStatus.PUBLISHED.value

    def test_publishing_does_not_open_the_shop(self, db: Session, agent: list[str]) -> None:
        """
        Two different decisions. A seller who has never seen their storefront
        should not discover it is live because they priced a shirt.
        """
        seller = self._priced(db)
        assert seller.is_published is False

        said = say(db, "publish")

        db.refresh(seller)
        assert seller.is_published is False
        assert "still closed" in said

    def test_an_unpriced_draft_is_not_published(self, db: Session, agent: list[str]) -> None:
        """
        The publish gate holds in the chat. An unpriced item cannot go live
        here any more than it can from the workspace.
        """
        seller_with_number(db)
        say(db, "New arrivals", media=2)
        say(db, "1800")
        say(db, "skip")

        say(db, "publish")

        belt = db.scalar(select(Product).where(Product.title == "Beaded Belt"))
        assert belt is not None
        assert belt.status == ProductStatus.DRAFT.value

    def test_publish_with_nothing_priced_says_so(self, db: Session) -> None:
        seller_with_number(db)

        assert "Nothing is priced" in say(db, "publish")


class TestOpeningTheShop:
    def test_open_makes_the_storefront_reachable(self, db: Session, agent: list[str]) -> None:
        seller = seller_with_number(db)
        say(db, "New stock", media=1)
        say(db, "1800")
        say(db, "publish")

        said = say(db, "open")

        db.refresh(seller)
        assert seller.is_published is True
        assert seller.slug in said

    def test_it_refuses_with_nothing_published(self, db: Session) -> None:
        seller = seller_with_number(db)

        said = say(db, "open")

        db.refresh(seller)
        assert seller.is_published is False
        assert "Publish something first" in said

    def test_a_seller_is_recognised_by_the_very_number_a_live_shop_needs(self, db: Session) -> None:
        """
        Why there is no "you have no WhatsApp number" branch here.

        The database refuses a published shop without one. That rail cannot
        fire on this path: a seller is RECOGNISED by their WhatsApp number, so
        one without a number is never seen as a seller to begin with — they get
        the stranger greeting instead. The guard belongs in the workspace,
        where a shop can be opened by someone signed in with an email.
        """
        seller = make_seller(db, whatsapp_number=None)
        make_product(
            db,
            seller,
            title="Ankara Shirt",
            price_kes=1800,
            status=ProductStatus.PUBLISHED.value,
        )
        db.flush()

        said = say(db, "open")

        db.refresh(seller)
        assert seller.is_published is False
        assert "Open a shop's link" in said


class TestSellersAndBuyersDoNotCollide:
    def test_a_buyer_typing_a_number_still_browses(self, db: Session, agent: list[str]) -> None:
        """
        The seller branch is keyed on owning a shop. A buyer who happens to
        type 1800 must still be shopping.
        """
        seller = make_seller(db, whatsapp_number="254700999888")
        seller.is_published = True
        make_product(
            db,
            seller,
            title="Ankara Shirt",
            price_kes=1500,
            stock=3,
            category="Fashion",
            status=ProductStatus.PUBLISHED.value,
        )
        db.flush()

        buyer = "254733222111"
        handle(db, buyer, f"shop {seller.slug}")
        said = "\n".join(r.body for r in handle(db, buyer, "1").replies)

        assert "Ankara Shirt" in said

    def test_publish_from_a_stranger_does_nothing(self, db: Session) -> None:
        """A word that acts on a shop must never work for somebody without one."""
        seller = seller_with_number(db)
        make_product(db, seller, title="Ankara Shirt", price_kes=1800)

        handle(db, "254700000000", "publish")

        product = db.scalar(select(Product).where(Product.seller_id == seller.id))
        assert product is not None
        assert product.status == ProductStatus.DRAFT.value
