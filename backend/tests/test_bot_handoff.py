"""
When the shop is asked something only the shop knows.

THE RULE THIS FILE ENFORCES: there is no "I don't know". A buyer asking "do you
deliver to Kisumu?" is asking something the system genuinely cannot answer — we
hold no delivery zones, no fees, no lead times — and the two bad options are
guessing (a promise on the buyer's screen the seller never made) and shrugging
(a shop that cannot answer its own customers). So it goes to the seller.

WHAT IS BEING PROVEN HERE:

    a question the system cannot answer reaches the SELLER, not a shrug
    the buyer's place in the conversation survives asking one
    the seller's words reach the buyer VERBATIM, attributed to their shop
    one shop cannot answer another shop's customer
    a question about the item on screen is answered from the ITEM, not the model

THE MODEL IS FAKED throughout — tests never hit a paid API. What is real is
every rule around it.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.understand import UnderstandingError
from app.config import get_settings
from app.models import BuyerQuestion, ConversationState, ProductStatus, Seller
from app.schemas.conversation import Intent, Understanding
from app.services import bot
from app.services.bot import get_conversation, handle
from tests.factories import make_payment_method, make_product, make_seller

SHOP_PHONE = "254712345678"
BUYER_PHONE = "254700111222"
OTHER_SHOP_PHONE = "254733000111"


def reading(intent: Intent, **fields: Any) -> Understanding:
    values: dict[str, Any] = {"intent": intent, "confidence": 0.9}
    values.update(fields)
    return Understanding.model_validate(values)


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[Understanding | None]:
    """A model whose answers a test queues up in advance."""
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

    monkeypatch.setattr(bot, "get_understander", lambda: Fake())
    return queue


def a_shop(db: Session) -> Seller:
    seller = make_seller(
        db,
        is_published=True,
        display_name="Vitabu Bora",
        slug="vitabu-bora",
        whatsapp_number=SHOP_PHONE,
    )
    make_payment_method(db, seller)
    make_product(
        db,
        seller,
        title="Maasai Beaded Sandals",
        price_kes=1200,
        status=ProductStatus.PUBLISHED.value,
        sizes=["37", "38", "39", "40"],
        description="Handmade leather, Maasai beadwork.",
        stock=5,
    )
    return seller


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


class TestTheQuestionReachesTheSeller:
    def test_a_question_we_cannot_answer_is_handed_over(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        seller = a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(reading(Intent.FOR_THE_SELLER, question="do you deliver to Kisumu?"))

        outcome = handle(db, BUYER_PHONE, "do you deliver to Kisumu?")

        # The buyer is told it was ASKED, never that it was answered.
        assert "Let me ask *Vitabu Bora*" in screen(outcome.replies)

        # And the seller has it, with the action attached.
        assert len(outcome.notify) == 1
        to, alert = outcome.notify[0]
        assert to == SHOP_PHONE
        assert "do you deliver to Kisumu?" in alert.body
        assert alert.buttons is not None
        assert alert.buttons[0][0].startswith("answer:")

        row = db.scalar(select(BuyerQuestion))
        assert row is not None
        assert row.is_open
        assert row.seller_id == seller.id
        assert row.buyer_phone == BUYER_PHONE

    def test_it_never_answers_on_the_sellers_behalf(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """
        THE WHOLE POINT. Even when the model offers a plausible answer, it is
        not shown — a sentence about delivery is a commitment on somebody
        else's business.
        """
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(
            reading(
                Intent.FOR_THE_SELLER,
                question="how much is delivery?",
                reply="Delivery is 300 bob anywhere in Kenya.",
            )
        )

        said = screen(handle(db, BUYER_PHONE, "how much is delivery?").replies)

        assert "300" not in said
        assert "Let me ask" in said

    def test_asking_does_not_cost_the_buyer_their_place(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """Somebody halfway through choosing a size is still choosing a size."""
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        handle(db, BUYER_PHONE, "sandals")
        handle(db, BUYER_PHONE, "add")
        assert get_conversation(db, BUYER_PHONE).state == ConversationState.VARIANT

        model.append(reading(Intent.FOR_THE_SELLER, question="is it waterproof?"))
        handle(db, BUYER_PHONE, "is it waterproof?")

        assert get_conversation(db, BUYER_PHONE).state == ConversationState.VARIANT
        # And the size answer still lands afterwards.
        assert "Added" in screen(handle(db, BUYER_PHONE, "39").replies)

    def test_the_ask_button_needs_no_model_at_all(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """A buyer should never have to hope the model guesses right."""
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")

        handle(db, BUYER_PHONE, "ask")
        outcome = handle(db, BUYER_PHONE, "can you hold one until Friday?")

        row = db.scalar(select(BuyerQuestion))
        assert row is not None
        assert row.question == "can you hold one until Friday?"
        assert outcome.notify[0][0] == SHOP_PHONE
        assert model == [], "the button path must not spend a model call"


class TestTheAnswerComesBack:
    def _asked(self, db: Session, model: list[Understanding | None]) -> BuyerQuestion:
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(reading(Intent.FOR_THE_SELLER, question="do you deliver to Kisumu?"))
        handle(db, BUYER_PHONE, "do you deliver to Kisumu?")
        row = db.scalar(select(BuyerQuestion))
        assert row is not None
        return row

    def test_the_seller_answers_and_the_buyer_hears_it_verbatim(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        row = self._asked(db, model)

        handle(db, SHOP_PHONE, f"answer:{row.id}")
        outcome = handle(db, SHOP_PHONE, "Yes, 300 bob, arrives next day")

        db.refresh(row)
        assert row.answer == "Yes, 300 bob, arrives next day"
        assert not row.is_open

        assert len(outcome.notify) == 1
        to, message = outcome.notify[0]
        assert to == BUYER_PHONE
        # Attributed, so the buyer knows a person answered and which one.
        assert "*Vitabu Bora* says:" in message.body
        assert "Yes, 300 bob, arrives next day" in message.body

    def test_questions_lists_what_is_still_waiting(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        row = self._asked(db, model)

        replies = handle(db, SHOP_PHONE, "questions").replies

        assert "do you deliver to Kisumu?" in screen(replies)
        assert f"answer:{row.id}" in taps(replies)

    def test_nothing_waiting_is_a_sentence_not_an_empty_list(self, db: Session) -> None:
        a_shop(db)

        replies = handle(db, SHOP_PHONE, "questions").replies

        assert "Nobody is waiting on you" in screen(replies)
        assert taps(replies), "never a dead end"

    def test_another_shop_cannot_answer_it(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """
        THE ONE THAT WOULD HURT. Answering in another shop's name, about their
        stock, to their customer — and the buyer with no way to tell.
        """
        row = self._asked(db, model)
        intruder = make_seller(
            db, whatsapp_number=OTHER_SHOP_PHONE, slug="zuma", display_name="Zuma"
        )
        make_payment_method(db, intruder)

        replies = handle(db, OTHER_SHOP_PHONE, f"answer:{row.id}").replies

        db.refresh(row)
        assert row.is_open
        assert "can't find that question" in screen(replies)

    def test_answering_twice_is_refused(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        row = self._asked(db, model)
        handle(db, SHOP_PHONE, f"answer:{row.id}")
        handle(db, SHOP_PHONE, "Yes we do")

        replies = handle(db, SHOP_PHONE, f"answer:{row.id}").replies

        assert "already answered" in screen(replies)
        assert "Yes we do" in screen(replies)


class TestAQuestionAboutTheItemOnScreen:
    def test_it_is_answered_from_the_item(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """Every word out of the product row. Nothing generated, because what a
        thing is made of has to be true."""
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        handle(db, BUYER_PHONE, "sandals")
        model.append(reading(Intent.ABOUT_THIS_ITEM, question="is it real leather?"))

        replies = handle(db, BUYER_PHONE, "is it real leather?").replies

        assert "Handmade leather, Maasai beadwork." in screen(replies)
        assert "KES 1,200" in screen(replies)
        assert db.scalar(select(BuyerQuestion)) is None, "no hand-off was needed"

    def test_with_nothing_on_screen_it_becomes_a_question_for_the_shop(
        self, db: Session, model: list[Understanding | None]
    ) -> None:
        """The model can pick the wrong one of the two. Falling through to the
        seller is the safe direction to be wrong in."""
        a_shop(db)
        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        model.append(reading(Intent.ABOUT_THIS_ITEM, question="what is it made of?"))

        outcome = handle(db, BUYER_PHONE, "what is it made of?")

        assert db.scalar(select(BuyerQuestion)) is not None
        assert outcome.notify[0][0] == SHOP_PHONE
