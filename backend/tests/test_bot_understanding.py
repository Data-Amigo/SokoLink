"""
Reading what somebody meant, when the keywords did not match.

THE EXCHANGE THIS FILE EXISTS FOR, from a real handset:

    bot   What's your shop called?
    them  My shop is called Biggie Books
    bot   *My shop is called Biggie Books* is yours. 🎉

The whole sentence became the shop's name — permanently, because the slug is
derived from it. They corrected it twice, "Sorry I mean Biggie Books is my brand
name" and "A way to put my catalogue and my shop link", and got the same status
card both times, because neither phrase matched a keyword either.

THE MODEL IS ALWAYS FAKED HERE. Tests never hit a paid API — that is not a
preference, it is the difference between a suite you run on every commit and one
nobody runs. The fake satisfies the same call and returns the same validated
schema, so these fail for real reasons rather than for quota ones.

WHAT IS ACTUALLY BEING PROVEN. Not that Gemini can read English — that is
Google's problem and unfalsifiable here. These prove the rules AROUND it:

    a sentence reaches the model only after the free paths have failed
    the model's words are shown for small talk and NEVER for a fact
    a low-confidence reading is treated as no reading at all
    a dead model leaves the shop working exactly as it did before
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.understand import UnderstandingError
from app.config import get_settings
from app.models import Account, ProductStatus, Seller
from app.schemas.conversation import Intent, Understanding
from app.services import bot
from app.services.bot import handle
from tests.factories import make_payment_method, make_product, make_seller

SELLER_PHONE = "254712345678"
BUYER_PHONE = "254700111222"


def reading(intent: Intent, **fields: Any) -> Understanding:
    """What the model might have concluded."""
    values: dict[str, Any] = {"intent": intent, "confidence": 0.9}
    values.update(fields)
    return Understanding.model_validate(values)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[Understanding | None]:
    """
    A model whose answers a test queues up in advance.

    Returns the queue: append an ``Understanding`` and the next message that
    reaches the model gets it. Appending None makes the call fail, which is how
    the degradation tests work.
    """
    queue: list[Understanding | None] = []
    monkeypatch.setattr(get_settings(), "gemini_api_key", "test-key")

    class Fake:
        def read(self, message: str, *, context: str) -> Understanding:
            if not queue:
                raise AssertionError(f"the model was asked to read {message!r}, unexpectedly")
            answer = queue.pop(0)
            if answer is None:
                raise UnderstandingError("pretend the provider is down")
            return answer

    monkeypatch.setattr(bot.reading, "get_understander", lambda: Fake())
    return queue


def a_shop(db: Session) -> Seller:
    seller = make_seller(
        db,
        is_published=True,
        display_name="Vitabu Bora",
        slug="vitabu-bora",
        whatsapp_number=SELLER_PHONE,
    )
    make_payment_method(db, seller)
    make_product(
        db,
        seller,
        title="Revision Book",
        price_kes=600,
        status=ProductStatus.PUBLISHED.value,
        stock=4,
    )
    return seller


def screen(replies: list) -> str:  # type: ignore[type-arg]
    return "\n".join(r.body for r in replies)


class TestTheBiggieBooksBug:
    def test_the_name_is_read_out_of_the_sentence(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        model.append(reading(Intent.SHOP_NAME, name="Biggie Books"))
        phone = "254700999888"
        handle(db, phone, "hi")
        handle(db, phone, "sell")

        handle(db, phone, "My shop is called Biggie Books")

        account = db.scalar(select(Account).where(Account.phone == phone))
        assert account is not None
        assert account.seller is not None
        assert account.seller.display_name == "Biggie Books"
        assert account.seller.slug == "biggie-books"

    def test_without_a_model_the_raw_text_is_still_used(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """
        The shop must still be creatable when the model is unreachable. Worse
        naming beats no shop.
        """
        model.append(None)
        phone = "254700999888"
        handle(db, phone, "hi")
        handle(db, phone, "sell")

        handle(db, phone, "Biggie Books")

        account = db.scalar(select(Account).where(Account.phone == phone))
        assert account is not None
        assert account.seller is not None
        assert account.seller.display_name == "Biggie Books"

    def test_a_seller_can_correct_the_name_afterwards(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """They said "Sorry I mean Biggie Books is my brand name" and there was
        no way to act on it."""
        seller = make_seller(
            db, whatsapp_number=SELLER_PHONE, display_name="My shop is called Biggie Books"
        )
        model.append(reading(Intent.SHOP_NAME, name="Biggie Books"))

        replies = handle(db, SELLER_PHONE, "Sorry I mean Biggie Books is my brand name").replies

        db.refresh(seller)
        assert seller.display_name == "Biggie Books"
        assert "Biggie Books" in screen(replies)

    def test_renaming_an_open_shop_keeps_the_link_it_already_shared(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """
        THE SLUG IS PERMANENT ONCE PUBLISHED. It is in their status and their
        bio; re-deriving it would break every link they had handed out.
        """
        seller = a_shop(db)
        model.append(reading(Intent.SHOP_NAME, name="Biggie Books"))

        replies = handle(db, SELLER_PHONE, "actually call it Biggie Books").replies

        db.refresh(seller)
        assert seller.display_name == "Biggie Books"
        assert seller.slug == "vitabu-bora"
        assert "still ends in /vitabu-bora" in screen(replies)


class TestASellerAsking:
    def test_a_question_about_the_catalogue_is_answered(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """ "A way to put my catalogue and my shop link" got a status card,
        twice."""
        make_seller(db, whatsapp_number=SELLER_PHONE)
        model.append(reading(Intent.SELLER_ADD_STOCK))

        replies = handle(db, SELLER_PHONE, "A way to put my catalogue and my shop link").replies

        assert "Forward me a post" in screen(replies)

    def test_asking_about_orders_reaches_orders(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        seller = make_seller(db, whatsapp_number=SELLER_PHONE)
        make_payment_method(db, seller)
        model.append(reading(Intent.SELLER_ORDERS))

        replies = handle(db, SELLER_PHONE, "has anyone bought anything today?").replies

        assert "Nothing waiting on you" in screen(replies)

    def test_small_talk_is_answered_and_then_they_see_their_shop(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """A greeting deserves an answer AND the thing they came for."""
        make_seller(db, whatsapp_number=SELLER_PHONE, display_name="Vitabu Bora")
        model.append(reading(Intent.GREET, reply="Habari! Good to see you back."))

        replies = handle(db, SELLER_PHONE, "habari yako rafiki").replies

        assert "Habari! Good to see you back." in screen(replies)
        assert "Vitabu Bora" in screen(replies)


class TestTheModelNeverStatesAFact:
    def test_its_words_are_dropped_for_anything_but_conversation(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """
        THE LINE BETWEEN AN ASSISTANT AND A LIABILITY. A model writing "yes, we
        have that for 1,200" would be inventing a price it cannot see. Only code
        reading the database writes sentences about stock and money.
        """
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(
            reading(
                Intent.FIND_PRODUCT,
                query="revision book",
                reply="Yes! We have it for KES 99.",
            )
        )

        replies = handle(db, BUYER_PHONE, "do you sell anything for revising").replies

        assert "KES 99" not in screen(replies)
        assert "Revision Book" in screen(replies)
        assert "600" in screen(replies)

    def test_a_search_with_no_matches_says_so_plainly(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(reading(Intent.FIND_PRODUCT, query="bicycle"))

        replies = handle(db, BUYER_PHONE, "would you have a bicycle at all").replies

        assert "couldn't find anything" in screen(replies)
        assert any(r.buttons for r in replies), "never a dead end"


class TestWhenNotToSpend:
    def test_a_button_never_reaches_the_model(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """
        Ids are unambiguous, instant and free. The fake raises if it is called,
        so an empty queue proves nothing was spent.
        """
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")

        handle(db, BUYER_PHONE, "menu")
        handle(db, BUYER_PHONE, "cart")
        handle(db, SELLER_PHONE, "orders")

        assert model == [], "a queued answer was left unused"

    def test_a_literal_product_name_never_reaches_the_model(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """The free title search already answers it."""
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")

        replies = handle(db, BUYER_PHONE, "Revision Book").replies

        assert "Revision Book" in screen(replies)

    def test_a_budget_never_reaches_the_model(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """The regex already answers it, for nothing."""
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")

        replies = handle(db, BUYER_PHONE, "under 1000").replies

        assert "Revision Book" in screen(replies)


class TestItDegrades:
    def test_a_dead_model_leaves_the_shop_working(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(None)

        replies = handle(db, BUYER_PHONE, "hmm what about something else").replies

        assert screen(replies), "silence is the one unacceptable answer"

    def test_no_api_key_means_no_call_at_all(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shop with no model configured behaves exactly as it did before."""
        monkeypatch.setattr(get_settings(), "gemini_api_key", None)

        def explode() -> None:
            raise AssertionError("the model must not be constructed without a key")

        monkeypatch.setattr(bot.reading, "get_understander", explode)
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")

        assert screen(handle(db, BUYER_PHONE, "anything nice for a child").replies)

    def test_a_hesitant_reading_is_treated_as_none(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """A wrong confident answer sends somebody down the wrong path. Unsure
        is a correct answer, and it means fall back."""
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(Understanding.model_validate({"intent": Intent.CHECKOUT, "confidence": 0.2}))

        replies = handle(db, BUYER_PHONE, "mm").replies

        assert "Who is this order for" not in screen(replies)


class TestTheExactExchangeFromTheHandset:
    """
    Replayed message for message from a real thread, because every failure this
    file exists for was found by a person typing, not by a test.

        them  That is not my shop name please
        bot   Sure — what should it be called instead?
        them  My shop name should be Biggie Books
        bot   Changed. … is now *My shop name should be Biggie Books*.   ← wrong

    The second reply took the sentence whole. Knowing which question is
    outstanding tells us what the reply is ABOUT; it does not make the reply
    only the answer. That is the same bug the extraction was built for, and it
    was reintroduced inside the fix for it.
    """

    def test_asking_then_answering_in_a_sentence(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        seller = make_seller(
            db, whatsapp_number=SELLER_PHONE, display_name="My shop is called Biggie Books"
        )

        # "That is not my shop name please" — a rename with no new name in it.
        model.append(reading(Intent.SHOP_NAME))
        asked = handle(db, SELLER_PHONE, "That is not my shop name please").replies
        assert "what should it be called instead" in screen(asked).lower()

        # "My shop name should be Biggie Books" — the answer, wrapped.
        model.append(reading(Intent.SHOP_NAME, name="Biggie Books"))
        handle(db, SELLER_PHONE, "My shop name should be Biggie Books")

        db.refresh(seller)
        assert seller.display_name == "Biggie Books"

    def test_the_answer_still_lands_without_a_model(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """Renaming clumsily beats not being able to rename at all."""
        seller = make_seller(db, whatsapp_number=SELLER_PHONE, display_name="Wrong Name")

        model.append(reading(Intent.SHOP_NAME))
        handle(db, SELLER_PHONE, "that is not my shop name")

        model.append(None)  # the provider falls over on the follow-up
        handle(db, SELLER_PHONE, "Biggie Books")

        db.refresh(seller)
        assert seller.display_name == "Biggie Books"
