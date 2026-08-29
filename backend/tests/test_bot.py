"""
The shop as a conversation.

This is the buyer surface now. A web link could not be relied on: tapping a URL
hands an intent to the OS, and some Android skins route it to the default
browser however the message is built — verified on a real handset, with both a
plain link and a native CTA button. So the whole purchase happens in the thread.

WHAT THESE TESTS ARE REALLY FOR. A chat has no page to inspect and no back
button; the only interface is the last message on screen. Two things therefore
have to hold, and neither is visible from reading the code:

    the state survives   a bare "2" means nothing without what we last asked
    the basket survives  it is spread across many separate HTTP requests

The second one already broke once — see ``TestTheBasketSurvives``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, OrderStatus, Product, ProductStatus, Seller
from app.services.bot import handle
from tests.factories import make_payment_method, make_product, make_seller

PHONE = "254712345678"


def open_shop(db: Session, **overrides: Any) -> Seller:
    """A published shop that can take an order."""
    seller = make_seller(db, **overrides)
    seller.is_published = True
    make_payment_method(db, seller)
    db.flush()
    return seller


def stock(
    db: Session, seller: Seller, title: str, category: str | None = "Fashion", **overrides: Any
) -> Product:
    """One published, priced product."""
    values: dict[str, Any] = {
        "title": title,
        "category": category,
        "price_kes": 1500,
        "stock": 5,
        "status": ProductStatus.PUBLISHED.value,
        "platform_post_id": f"71{abs(hash(title)) % 10**16:016d}",
    }
    values.update(overrides)
    return make_product(db, seller, **values)


def say(db: Session, text: str, phone: str = PHONE) -> str:
    """Send one message and return everything the bot said, joined."""
    return "\n".join(r.body for r in handle(db, phone, text).replies)


class TestFindingTheShop:
    """
    The shareable link is wa.me/<bot>?text=shop <slug>, so the first message
    names the shop. That link opens WhatsApp itself — a deep link, not a URL,
    which is the whole reason this surface exists.
    """

    def test_the_prefilled_message_opens_that_shop(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")

        said = say(db, f"shop {seller.slug}")

        assert seller.display_name in said
        assert "Fashion" in said

    def test_an_unknown_shop_says_so(self, db: Session) -> None:
        assert "can't find that shop" in say(db, "shop no-such-shop")

    def test_a_closed_shop_is_not_reachable(self, db: Session) -> None:
        """
        THE PUBLISH GATE HOLDS IN THE CHAT TOO. A surface that ignored it would
        be a second way to go live that the review queue never sees.
        """
        seller = make_seller(db)
        stock(db, seller, "Secret Thing", "Fashion")
        assert seller.is_published is False

        assert "can't find that shop" in say(db, f"shop {seller.slug}")

    def test_someone_with_no_shop_is_told_how_to_start(self, db: Session) -> None:
        assert "Open a shop's link" in say(db, "hello")


class TestBrowsing:
    def test_a_number_picks_a_category(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        stock(db, seller, "Canvas Sneakers", "Shoes")
        say(db, f"shop {seller.slug}")

        # Categories come back sorted, so Fashion is 1 and Shoes is 2.
        said = say(db, "2")

        assert "Canvas Sneakers" in said
        assert "Ankara Shirt" not in said

    def test_a_number_then_opens_the_product(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion", description="Cotton, made in Nairobi")
        say(db, f"shop {seller.slug}")
        say(db, "1")

        said = say(db, "1")

        assert "Ankara Shirt" in said
        assert "Cotton, made in Nairobi" in said

    def test_the_product_photo_is_sent(self, db: Session) -> None:
        """A catalogue without pictures is a price list. The image is the point."""
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion", cover_url="https://example.test/shirt.jpg")
        say(db, f"shop {seller.slug}")
        say(db, "1")

        replies = handle(db, PHONE, "1").replies

        assert replies[0].media_url == "https://example.test/shirt.jpg"

    def test_gibberish_re_offers_the_menu(self, db: Session) -> None:
        """
        A buyer who sends something unreadable has not failed to parse — they
        cannot see their options. "I didn't understand" leaves them exactly
        where they were stuck.
        """
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")

        assert "What are you looking for?" in say(db, "asdfghjkl")

    def test_a_shop_with_no_categories_lists_products_directly(self, db: Session) -> None:
        """A seller who never typed a category must not cost their buyer a
        dead screen."""
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", category=None)

        assert "Ankara Shirt" in say(db, f"shop {seller.slug}")


class TestTheBasketSurvives:
    """
    THIS IS THE ONE THAT ALREADY BROKE.

    ``get_or_create_cart`` mints a NEW token whenever it is not given one it
    recognises; the web flow relies on that and writes the token back to a
    cookie. A chat has no cookie, so the first version passed a made-up token,
    which was never found — every message silently got a fresh empty basket.
    The bot said "Added" and checkout said "your basket is empty".
    """

    def test_an_added_item_is_still_there_next_message(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")
        say(db, "1")

        say(db, "add")
        said = say(db, "cart")

        assert "Ankara Shirt" in said
        assert "KSh 1,500" in said

    def test_two_items_both_survive(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion", price_kes=1500)
        stock(db, seller, "Denim Jacket", "Fashion", price_kes=3000)
        say(db, f"shop {seller.slug}")

        # After "add" the buyer is looking at their basket, so getting back to
        # a numbered product list means walking menu -> category -> item. The
        # indexes are positions in that list, not a stable product order:
        # get_public_products puts in-stock first, then newest.
        for index in ("1", "2"):
            say(db, "menu")
            say(db, "1")
            say(db, index)
            say(db, "add")

        said = say(db, "cart")

        assert "Ankara Shirt" in said
        assert "Denim Jacket" in said
        assert "KSh 4,500" in said

    def test_clear_empties_it(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")
        say(db, "1")
        say(db, "add")

        say(db, "clear")

        assert "empty" in say(db, "cart").lower()


class TestCheckout:
    def _with_one_item(self, db: Session) -> Seller:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")
        say(db, "1")
        say(db, "add")
        return seller

    def test_it_asks_for_a_name_and_a_place(self, db: Session) -> None:
        """
        WhatsApp gives us the phone, which is both the M-Pesa line and the way
        to reach them. It does not give us a name or an address.
        """
        self._with_one_item(db)

        said = say(db, "checkout")

        assert "name" in said.lower()
        assert "deliver" in said.lower()

    def test_it_places_a_real_order(self, db: Session) -> None:
        self._with_one_item(db)
        say(db, "checkout")

        said = say(db, "Akinyi Otieno, Kasarani")

        order = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert order is not None
        assert order.buyer_name == "Akinyi Otieno"
        assert order.delivery_address == "Kasarani"
        assert order.buyer_phone == PHONE
        assert order.reference in said

    def test_it_names_where_the_money_goes(self, db: Session) -> None:
        """The buyer pays the seller directly. If this number is missing or
        wrong, nothing else in the flow matters."""
        seller = self._with_one_item(db)
        say(db, "checkout")

        said = say(db, "Akinyi Otieno, Kasarani")

        assert seller.payment_method is not None
        assert seller.payment_method.number in said

    def test_a_malformed_answer_asks_again(self, db: Session) -> None:
        self._with_one_item(db)
        say(db, "checkout")

        said = say(db, "Kasarani")

        assert "comma" in said.lower()
        assert db.scalars(select(Order)).first() is None

    def test_checkout_refuses_when_the_seller_cannot_be_paid(self, db: Session) -> None:
        """
        Taking an order the seller has no way to be paid for is worse than
        refusing it — the buyer sends money nowhere.
        """
        seller = make_seller(db)
        seller.is_published = True
        stock(db, seller, "Ankara Shirt", "Fashion")
        db.flush()
        say(db, f"shop {seller.slug}")
        say(db, "1")
        say(db, "1")
        say(db, "add")

        assert "hasn't set up payments" in say(db, "checkout")

    def test_an_empty_basket_cannot_check_out(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")

        assert "empty" in say(db, "checkout").lower()


class TestPaying:
    def _ordered(self, db: Session) -> Order:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")
        say(db, "1")
        say(db, "add")
        say(db, "checkout")
        say(db, "Akinyi Otieno, Kasarani")
        order = db.scalars(select(Order).order_by(Order.id.desc())).first()
        assert order is not None
        return order

    def test_a_code_is_recorded_as_a_claim_not_a_payment(self, db: Session) -> None:
        """
        THE RULE THE WHOLE PAYMENT MODEL RESTS ON. Only the seller moves an
        order to paid, after checking their own M-Pesa. Telling a buyer their
        payment is confirmed here would be a lie they would act on.
        """
        order = self._ordered(db)

        said = say(db, "SLK7XA2B9C")

        db.refresh(order)
        assert order.status == OrderStatus.AWAITING_CONFIRMATION.value
        assert order.payments[-1].claimed_code == "SLK7XA2B9C"
        assert "confirm" in said.lower()

    def test_something_that_is_not_a_code_is_rejected(self, db: Session) -> None:
        order = self._ordered(db)

        said = say(db, "i have paid")

        db.refresh(order)
        assert "doesn't look like an M-Pesa code" in said
        assert order.payments == []


class TestTheWordsThatAlwaysWork:
    """
    A buyer who is lost cannot be required to first find the screen where
    escaping is offered, so these are checked before state.
    """

    def test_menu_works_from_the_middle_of_checkout(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")
        say(db, "1")
        say(db, "add")
        say(db, "checkout")

        assert "What are you looking for?" in say(db, "menu")

    def test_help_lists_the_commands(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")

        said = say(db, "help")

        for word in ("menu", "cart", "checkout", "clear"):
            assert word in said

    def test_a_swahili_greeting_opens_the_menu(self, db: Session) -> None:
        """Sellers and buyers greet in Swahili and Sheng, not in English
        imperatives."""
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")

        assert "What are you looking for?" in say(db, "niaje")


class TestTwoBuyersDoNotCollide:
    def test_each_phone_has_its_own_basket(self, db: Session) -> None:
        """The phone is the identity here. One shared basket would be the
        worst possible bug on this surface."""
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        other = "254799999999"

        for phone in (PHONE, other):
            say(db, f"shop {seller.slug}", phone=phone)
            say(db, "1", phone=phone)
            say(db, "1", phone=phone)

        say(db, "add", phone=PHONE)

        assert "Ankara Shirt" in say(db, "cart", phone=PHONE)
        assert "empty" in say(db, "cart", phone=other).lower()

    def test_the_conversation_state_is_per_phone(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")

        # A second buyer arriving fresh must not inherit the first one's screen.
        assert "Open a shop's link" in say(db, "hi", phone="254788888888")


class TestSoldOut:
    def test_a_sold_out_item_says_so_and_cannot_be_added(self, db: Session) -> None:
        """
        Sold-out items stay listed on purpose: a buyer who saw it on TikTok and
        finds nothing assumes the shop is dead.
        """
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion", stock=0)
        say(db, f"shop {seller.slug}")
        say(db, "1")

        said = say(db, "1")
        assert "sold out" in said.lower()

        say(db, "add")
        assert "empty" in say(db, "cart").lower()


class TestNativeComponents:
    """
    Buttons and list rows, which Meta draws as real tap targets.

    THE TEXT NEVER GOES AWAY. Twilio cannot render a component, and neither can
    some clients, so every reply's body still names every option. A reply whose
    body read only "Choose:" would be a dead end everywhere the buttons do not
    appear — which is most of the places this has to work.
    """

    def test_the_menu_carries_rows_and_still_reads_as_text(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        stock(db, seller, "Canvas Sneakers", "Shoes")

        reply = handle(db, PHONE, f"shop {seller.slug}").replies[0]

        assert reply.rows is not None
        assert [r[0] for r in reply.rows] == ["cat:Fashion", "cat:Shoes"]
        # And the same options are readable without any component at all.
        assert "1. Fashion" in reply.body
        assert "2. Shoes" in reply.body

    def test_a_product_row_carries_the_price_as_its_description(self, db: Session) -> None:
        """
        A row title has 24 characters and the item's name needs all of them.
        The price is the second line a buyer reads, so it belongs there.
        """
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion", price_kes=1800)
        say(db, f"shop {seller.slug}")

        reply = handle(db, PHONE, "1").replies[0]

        assert reply.rows is not None
        row_id, title, description = reply.rows[0]
        assert title == "Ankara Shirt"
        assert "1,800" in description

    def test_a_product_offers_three_buttons(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")

        reply = handle(db, PHONE, "1").replies[0]

        assert reply.buttons is not None
        assert [b[0] for b in reply.buttons] == ["add", "menu", "cart"]

    def test_a_sold_out_product_does_not_offer_add(self, db: Session) -> None:
        """Offering a control that cannot work is worse than offering none."""
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion", stock=0)
        say(db, f"shop {seller.slug}")
        say(db, "1")

        reply = handle(db, PHONE, "1").replies[0]

        assert reply.buttons is not None
        assert "add" not in [b[0] for b in reply.buttons]

    def test_never_more_than_three_buttons(self, db: Session) -> None:
        """Meta rejects the whole message at four, and the buyer sees nothing."""
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")
        say(db, "1")

        for text in ("1", "cart"):
            reply = handle(db, PHONE, text).replies[0]
            if reply.buttons:
                assert len(reply.buttons) <= 3


class TestTappingVersusTyping:
    """
    A tap returns the id we set; typing returns a word. Both must reach the
    same place, because a buyer on a client that draws no buttons is still a
    buyer.
    """

    def test_tapping_a_category_row_lists_it(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Canvas Sneakers", "Shoes")
        say(db, f"shop {seller.slug}")

        said = say(db, "cat:Shoes")

        assert "Canvas Sneakers" in said

    def test_tapping_a_product_row_opens_it(self, db: Session) -> None:
        seller = open_shop(db)
        product = stock(db, seller, "Canvas Sneakers", "Shoes")
        say(db, f"shop {seller.slug}")

        said = say(db, f"prod:{product.id}")

        assert "Canvas Sneakers" in said

    def test_a_product_id_from_another_shop_is_refused(self, db: Session) -> None:
        """
        THE ID IS GUESSABLE. One shop's chat must never render another shop's
        stock just because a buyer sent an integer.
        """
        mine = open_shop(db)
        stock(db, mine, "Ankara Shirt", "Fashion")
        theirs = open_shop(db, slug="someone-else", display_name="Someone Else")
        secret = stock(db, theirs, "Their Secret Bag", "Bags")
        say(db, f"shop {mine.slug}")

        said = say(db, f"prod:{secret.id}")

        assert "Their Secret Bag" not in said

    def test_a_malformed_id_falls_back_to_the_menu(self, db: Session) -> None:
        seller = open_shop(db)
        stock(db, seller, "Ankara Shirt", "Fashion")
        say(db, f"shop {seller.slug}")

        assert "What are you looking for?" in say(db, "prod:not-a-number")
