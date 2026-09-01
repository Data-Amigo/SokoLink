"""
The buyer's journey, end to end, and the four things that made it feel robotic.

WHAT THIS FILE EXISTS TO PREVENT. A real test on a real handset walked from a
product card to a dead end in under a minute, and every step of it was
reproducible here and had never been tested:

    adding to the basket sent a sentence with NO buttons, because a slice
    written when the basket was two messages silently emptied itself when it
    became one

    "Check out" did not match "checkout", so a buyer who typed rather than
    tapped fell through to the fallback

    the fallback re-greeted them — shop name, "Karibu!", the whole arrival —
    in the middle of a purchase

    the card printed "Sizes: 37, 38, 39, 40" and then added to the basket
    without ever asking which, so the seller received an order for sandals
    with no size on it

None of these were subtle. All four survived because the tests checked that the
right FUNCTION was called and never read what a person would actually see.

SO THE ASSERTIONS HERE ARE ABOUT THE SCREEN. What is written, what can be
tapped, and what is not said twice.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ConversationState, Order, OrderItem, Product, ProductStatus, Seller
from app.services.bot import get_conversation, handle
from tests.factories import make_payment_method, make_product, make_seller

BUYER = "254700111222"


def say(db: Session, text: str, phone: str = BUYER) -> list:  # type: ignore[type-arg]
    return handle(db, phone, text).replies


def screen(replies: list) -> str:  # type: ignore[type-arg]
    return "\n".join(r.body for r in replies)


def taps(replies: list) -> list[str]:  # type: ignore[type-arg]
    out: list[str] = []
    for reply in replies:
        for button_id, _ in reply.buttons or []:
            out.append(button_id)
        for row_id, _, _ in reply.rows or []:
            out.append(row_id)
    return out


def a_shop(db: Session, **overrides: object) -> Seller:
    seller = make_seller(
        db, is_published=True, display_name="Vitabu Bora", slug="vitabu-bora", **overrides
    )
    make_payment_method(db, seller)
    return seller


def sized_item(db: Session, seller: Seller) -> Product:
    return make_product(
        db,
        seller,
        title="Maasai Beaded Sandals",
        price_kes=1200,
        status=ProductStatus.PUBLISHED.value,
        sizes=["37", "38", "39", "40"],
        stock=5,
    )


def plain_item(db: Session, seller: Seller) -> Product:
    return make_product(
        db,
        seller,
        title="Leather Tote Bag",
        price_kes=2800,
        status=ProductStatus.PUBLISHED.value,
        platform_post_id="7100000000000009999",
        stock=5,
    )


class TestAddingToTheBasket:
    def test_it_never_leaves_the_buyer_with_nothing_to_tap(self, db: Session) -> None:
        """
        THE BUG FROM THE HANDSET. `_show_cart(...)[1:]` was written when the
        basket was two messages. It became one, the slice went empty, and the
        buyer got a bare sentence and had to type their way out.
        """
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")

        replies = say(db, "add")

        assert "checkout" in taps(replies)
        assert "Leather Tote Bag" in screen(replies)
        assert "2,800" in screen(replies)

    def test_the_acknowledgement_and_the_basket_are_one_message(self, db: Session) -> None:
        """A shopkeeper says "got it, that's two things, shall we ring it up" —
        not "got it", then silence."""
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")

        replies = say(db, "add")

        assert len(replies) == 1
        assert "Added" in replies[0].body
        assert "Your basket" in replies[0].body


class TestTheSizeIsAsked:
    def test_a_sized_product_asks_before_the_basket(self, db: Session) -> None:
        """
        THE HOLE. The card printed the sizes and then added the item without
        asking which, so the seller opened an order for sandals with no size and
        had to go back to the buyer to find out.
        """
        seller = a_shop(db)
        item = sized_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")

        replies = say(db, "add")

        assert "Which size" in screen(replies)
        assert taps(replies) == ["size:37", "size:38", "size:39", "size:40"]
        assert get_conversation(db, BUYER).state == ConversationState.VARIANT

    def test_the_chosen_size_reaches_the_order(self, db: Session) -> None:
        seller = a_shop(db)
        item = sized_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")

        say(db, "size:39")
        say(db, "checkout")
        say(db, "Akinyi Otieno")
        say(db, "collect")

        line = db.scalar(select(OrderItem))
        assert line is not None
        assert line.selected_variant == "39"

    def test_a_typed_size_works_too(self, db: Session) -> None:
        """A buyer on a handset that draws no picker still has to be able to buy."""
        seller = a_shop(db)
        item = sized_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")

        replies = say(db, "40")

        assert "Added" in screen(replies)
        assert "(40)" in screen(replies)

    def test_a_size_the_shop_does_not_stock_is_refused(self, db: Session) -> None:
        """Matched against what we actually offered. A size we never listed must
        not reach the order as though the seller had it."""
        seller = a_shop(db)
        item = sized_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")

        replies = say(db, "44")

        assert "Which size" in screen(replies)
        assert get_conversation(db, BUYER).state == ConversationState.VARIANT

    def test_a_product_with_no_sizes_is_not_asked(self, db: Session) -> None:
        """The question is only worth asking when there is a choice to make."""
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")

        replies = say(db, "add")

        assert "Which size" not in screen(replies)
        assert "Added" in screen(replies)


class TestItDoesNotRepeatItself:
    def test_arriving_is_greeted(self, db: Session) -> None:
        seller = a_shop(db)
        plain_item(db, seller)

        arrival = screen(say(db, "Shop Vitabu Bora"))

        assert "Karibu" in arrival
        assert "Vitabu Bora" in arrival

    def test_the_welcome_says_who_what_and_how_many(self, db: Session) -> None:
        """
        A buyer decides whether a shop is worth browsing in about two seconds,
        and decides it here. The welcome names the shop, says what it sells,
        counts the stock, and shows how to ask — "What are you looking for?" on
        its own is a form.
        """
        seller = a_shop(db)
        seller.bio = "Vitabu Bora sells books and learning materials."
        plain_item(db, seller)
        sized_item(db, seller)
        db.flush()

        arrival = screen(say(db, "Shop Vitabu Bora"))

        assert "Karibu Vitabu Bora" in arrival
        assert "books and learning materials" in arrival
        assert "*2 items* available" in arrival

    def test_the_description_is_the_sellers_own_words(self, db: Session) -> None:
        """Nothing we generate describes somebody's shop better than they can."""
        seller = a_shop(db)
        seller.bio = "Nairobi's home for second-hand novels."
        plain_item(db, seller)
        db.flush()

        assert "Nairobi's home for second-hand novels." in screen(say(db, "Shop Vitabu Bora"))

    def test_with_no_bio_it_describes_the_shop_from_its_categories(self, db: Session) -> None:
        """Those are the seller's words too — they typed them onto the products."""
        seller = a_shop(db)
        make_product(
            db,
            seller,
            title="Atlas",
            category="Books",
            price_kes=900,
            status=ProductStatus.PUBLISHED.value,
            platform_post_id="7100000000000007777",
        )
        assert seller.bio is None

        arrival = screen(say(db, "Shop Vitabu Bora"))

        assert "Vitabu Bora sells books" in arrival

    def test_it_teaches_the_buyer_they_can_just_ask(self, db: Session) -> None:
        """A person handed a numbered menu assumes numbers are all it takes.
        The examples are the cheapest way to say "talk to me normally"."""
        seller = a_shop(db)
        plain_item(db, seller)

        arrival = screen(say(db, "Shop Vitabu Bora"))

        assert "you can say things like" in arrival
        assert "under 1000" in arrival

    def test_saying_hi_is_answered_with_a_greeting(self, db: Session) -> None:
        """
        THE OVER-CORRECTION. Repetition was killed by greeting almost never, so
        a buyer opening with "Hi" got a bare numbered list belonging to nobody —
        ruder than the repetition it replaced. A greeting is answered with a
        greeting.
        """
        seller = a_shop(db)
        plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, "menu")

        replies = say(db, "Hi")

        assert "Karibu" in screen(replies)
        assert "Vitabu Bora" in screen(replies)

    def test_tapping_keep_shopping_is_not_a_greeting(self, db: Session) -> None:
        """The other half of the rule. Somebody mid-basket did not say hello,
        and introducing the shop to them again is the repetition."""
        seller = a_shop(db)
        plain_item(db, seller)
        say(db, "Shop Vitabu Bora")

        assert "Karibu" not in screen(say(db, "menu"))
        assert "Karibu" not in screen(say(db, "keep shopping"))

    def test_an_unreadable_word_does_not_restart_the_conversation(self, db: Session) -> None:
        """
        THE ONE FROM THE SCREENSHOT. A buyer mid-purchase typed something we
        could not read and was greeted from the top, as though they had just
        walked in.
        """
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")

        replies = say(db, "asdkjh")

        assert "Karibu" not in screen(replies)

    def test_the_product_card_does_not_narrate_its_own_buttons(self, db: Session) -> None:
        """ "Send *add* to put this in your basket" printed directly above a
        button saying Add to basket. Saying it twice is what a form does."""
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")

        replies = say(db, f"prod:{item.id}")

        assert "Send *add*" not in screen(replies)
        assert "add" in taps(replies)


class TestCheckout:
    def test_check_out_with_a_space_is_understood(self, db: Session) -> None:
        """The button sends an id, so this only ever bit somebody typing — which
        is exactly the buyer least able to recover from it."""
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")

        replies = say(db, "Check out")

        assert "Who is this order for" in screen(replies)

    def test_one_question_at_a_time(self, db: Session) -> None:
        """
        It used to ask for "your name and where to deliver, separated by a
        comma" — a form pasted into a chat, which fails for anybody whose estate
        has a comma in its name.
        """
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")

        name_step = say(db, "checkout")
        assert "Who is this order for" in screen(name_step)

        delivery_step = say(db, "Akinyi Otieno")
        assert taps(delivery_step) == ["deliver", "collect"]

        address_step = say(db, "deliver")
        assert "Where should it go" in screen(address_step)

        say(db, "Kasarani, near Hunters")

        order = db.scalar(select(Order))
        assert order is not None
        assert order.buyer_name == "Akinyi Otieno"
        assert order.delivery_address == "Kasarani, near Hunters"

    def test_collecting_asks_for_no_address(self, db: Session) -> None:
        """A choice that was never offered: checkout demanded an address from
        everybody, including buyers who meant to collect."""
        seller = a_shop(db)
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")
        say(db, "checkout")
        say(db, "Akinyi Otieno")

        replies = say(db, "collect")

        order = db.scalar(select(Order))
        assert order is not None
        assert order.delivery_address is None
        assert order.reference in screen(replies)

    def test_a_shop_that_cannot_be_paid_says_so_before_asking_anything(self, db: Session) -> None:
        """Finding out after typing a name and an address is the worst possible
        moment to be told."""
        seller = make_seller(db, is_published=True, display_name="Vitabu Bora", slug="vitabu-bora")
        item = plain_item(db, seller)
        say(db, "Shop Vitabu Bora")
        say(db, f"prod:{item.id}")
        say(db, "add")

        replies = say(db, "checkout")

        assert "hasn't set up M-Pesa" in screen(replies)
        assert db.scalar(select(Order)) is None


class TestOneNumberIsNotOneRole:
    """
    The bot number is shared, and the same person runs a shop on Monday and buys
    shoes on Tuesday. Deciding by phone number alone answered the wrong
    question: identity is not context.
    """

    def test_a_seller_opening_another_shop_is_told_they_switched(self, db: Session) -> None:
        owner = make_seller(
            db, whatsapp_number="254733000111", slug="book-lounge", display_name="Book Lounge"
        )
        make_payment_method(db, owner)
        other = a_shop(db)
        plain_item(db, other)

        replies = handle(db, "254733000111", "Shop Vitabu Bora").replies

        assert "shopping at *Vitabu Bora*" in screen(replies)
        assert "my shop" in screen(replies)
        assert "Book Lounge" in screen(replies)

    def test_they_can_shop_there_like_anybody_else(self, db: Session) -> None:
        owner = make_seller(
            db, whatsapp_number="254733000111", slug="book-lounge", display_name="Book Lounge"
        )
        make_payment_method(db, owner)
        other = a_shop(db)
        item = plain_item(db, other)

        handle(db, "254733000111", "Shop Vitabu Bora")
        handle(db, "254733000111", f"prod:{item.id}")
        replies = handle(db, "254733000111", "add").replies

        assert "Added" in screen(replies)

    def test_my_shop_takes_them_home_again(self, db: Session) -> None:
        """A seller stuck inside another shop's conversation with no exit is
        worse than never having let them browse."""
        owner = make_seller(
            db, whatsapp_number="254733000111", slug="book-lounge", display_name="Book Lounge"
        )
        make_payment_method(db, owner)
        a_shop(db)

        handle(db, "254733000111", "Shop Vitabu Bora")
        replies = handle(db, "254733000111", "my shop").replies

        assert "Book Lounge" in screen(replies)
        assert "Vitabu Bora" not in screen(replies)
        assert get_conversation(db, "254733000111").seller_id is None


class TestItTakesThemAtTheirWord:
    """
    Somebody who types "handbag" has said exactly what they want. Sending them
    to a category list to find it themselves is the machine asking the person to
    do the machine's job.
    """

    def test_naming_one_item_goes_straight_to_it(self, db: Session) -> None:
        seller = a_shop(db)
        plain_item(db, seller)
        say(db, "Shop Vitabu Bora")

        replies = say(db, "tote")

        assert "Leather Tote Bag" in screen(replies)
        assert "add" in taps(replies)

    def test_naming_something_broader_lists_the_matches(self, db: Session) -> None:
        seller = a_shop(db)
        plain_item(db, seller)
        make_product(
            db,
            seller,
            title="Woven Tote Basket",
            price_kes=1600,
            status=ProductStatus.PUBLISHED.value,
            platform_post_id="7100000000000008888",
            stock=3,
        )
        say(db, "Shop Vitabu Bora")

        replies = say(db, "tote")

        assert "Leather Tote Bag" in screen(replies)
        assert "Woven Tote Basket" in screen(replies)

    def test_a_word_that_matches_nothing_still_leaves_them_somewhere(self, db: Session) -> None:
        """
        The fallback has to stay: not finding a match is no reason to leave
        somebody with nothing.

        ASSERTED ON BEING OFFERED THE STOCK, not on a particular sentence. This
        shop's products carry no category, so the menu is the whole catalogue
        rather than a list of pills — checking for "What are you looking for?"
        was checking which BRANCH ran, when the rule is that the buyer can still
        get somewhere.
        """
        seller = a_shop(db)
        plain_item(db, seller)
        say(db, "Shop Vitabu Bora")

        replies = say(db, "helicopter")

        assert "Leather Tote Bag" in screen(replies)
        assert taps(replies), "a reply with nothing to tap is a dead end"


class TestABudgetIsAQuestion:
    """
    The welcome offers "What do you have under 1000?" as an example. An example
    that drops the buyer into a fallback teaches them the shop does not listen,
    so the suggestion and the feature have to ship together.
    """

    def test_it_answers_a_budget(self, db: Session) -> None:
        seller = a_shop(db)
        make_product(
            db,
            seller,
            title="Cheap Notebook",
            price_kes=200,
            status=ProductStatus.PUBLISHED.value,
            platform_post_id="7100000000000006666",
        )
        plain_item(db, seller)  # 2,800 — over the ceiling

        replies = say(db, "Shop Vitabu Bora")
        replies = say(db, "what do you have under 1000?")

        assert "Cheap Notebook" in screen(replies)
        assert "Leather Tote Bag" not in screen(replies)

    def test_several_ways_of_saying_it(self, db: Session) -> None:
        seller = a_shop(db)
        make_product(
            db,
            seller,
            title="Cheap Notebook",
            price_kes=200,
            status=ProductStatus.PUBLISHED.value,
            platform_post_id="7100000000000006666",
        )
        say(db, "Shop Vitabu Bora")

        for phrasing in ("below 500", "less than 900", "up to 1,000", "si zaidi ya 400"):
            assert "Cheap Notebook" in screen(say(db, phrasing)), phrasing

    def test_a_budget_is_not_matched_against_titles(self, db: Session) -> None:
        """ "under 1000" is money, not a product called "1000 Riddles"."""
        seller = a_shop(db)
        make_product(
            db,
            seller,
            title="1000 Riddles",
            price_kes=4000,
            status=ProductStatus.PUBLISHED.value,
            platform_post_id="7100000000000005555",
        )
        say(db, "Shop Vitabu Bora")

        assert "1000 Riddles" not in screen(say(db, "anything under 1000"))

    def test_nothing_in_budget_says_so_rather_than_ignoring_it(self, db: Session) -> None:
        """A menu that silently drops the number they named is worse than a no."""
        seller = a_shop(db)
        plain_item(db, seller)  # 2,800
        say(db, "Shop Vitabu Bora")

        replies = say(db, "under 500")

        assert "under *KES 500*" in screen(replies)
        assert taps(replies), "a dead end is never the answer"

    def test_an_unpriced_item_cannot_reach_a_buyer_at_all(self, db: Session) -> None:
        """
        The budget filter excludes NULL prices, and this test was written to
        prove it — by publishing an unpriced product, which the database
        refuses outright:

            ck_products_published_requires_price

        So the scenario cannot occur, and the guard in the query is belt and
        braces rather than the thing standing between a buyer and a wrong
        answer. Worth knowing which of the two is load-bearing.
        """
        seller = a_shop(db)

        # The factory flushes, so the rail fires inside this call rather than
        # on a later one.
        with pytest.raises(IntegrityError):
            make_product(
                db,
                seller,
                title="Mystery Item",
                price_kes=None,
                status=ProductStatus.PUBLISHED.value,
                platform_post_id="7100000000000004444",
            )
        db.rollback()
