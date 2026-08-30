"""
The seller's business, run entirely from the chat thread.

WHAT THIS FILE IS ACTUALLY ABOUT. A seller should be able to open WhatsApp and
answer the only question they have most mornings — has anybody paid me — and
then act on the answer. Before this, the thread could take stock and could not
take money: there was no way to see an order, and no way to confirm one. A
catalogue you cannot get paid for is not a shop.

THE GUARDRAILS ARE THE POINT, not the copy. Three of them earn their tests here:

    a shop cannot open with no way to pay it
    one seller cannot confirm another seller's order
    a seller who types something unreadable is never asked to identify themselves

The third sounds cosmetic and is not. Falling through to "are you here to sell,
or to shop?" tells somebody whose shop we are already running that we have
forgotten them, which is the single fastest way to lose their trust in it.

EVERY REPLY IS CHECKED FOR A WAY OUT. A chat has no menu bar — the last message
on screen is the entire interface — so a reply that states a fact and offers
nothing to tap is a dead end, and dead ends are how a seller decides the thing
is broken.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ConversationState,
    Order,
    OrderStatus,
    PaymentMethod,
    PaymentMethodKind,
    Product,
    ProductStatus,
    Seller,
)
from app.services.bot import get_conversation, handle
from app.services.cart import add_item, get_or_create_cart
from app.services.orders import claim_payment, place_order
from tests.factories import make_payment_method, make_product, make_seller

PHONE = "254712345678"
OTHER_PHONE = "254733000111"


def said(replies: list) -> str:  # type: ignore[type-arg]
    """Everything the bot put on screen, as one blob to search."""
    return "\n".join(r.body for r in replies)


def taps(replies: list) -> list[str]:  # type: ignore[type-arg]
    """Every id a person could tap across all the replies — buttons and rows."""
    out: list[str] = []
    for reply in replies:
        for button_id, _ in reply.buttons or []:
            out.append(button_id)
        for row_id, _, _ in reply.rows or []:
            out.append(row_id)
    return out


def stocked(db: Session, seller: Seller, *, price: int = 600, count: int = 1) -> list[Product]:
    """Items already in the shop, priced, the way a real catalogue looks."""
    products = []
    for i in range(count):
        products.append(
            make_product(
                db,
                seller,
                title=f"Item {i + 1}",
                price_kes=price,
                status=ProductStatus.PUBLISHED.value,
                platform_post_id=f"710000000000000{i:04d}",
            )
        )
    return products


def an_order_awaiting(db: Session, seller: Seller, *, code: str = "SJ4K2L9X") -> Order:
    """
    A real order that has reached the one state only the seller can move it out of.

    Built through the same services checkout uses, deliberately: an order
    hand-constructed in a test proves the chat works against a shape production
    never sends.
    """
    product = stocked(db, seller)[0]
    cart = get_or_create_cart(db, None, seller)
    add_item(db, cart, product.id, quantity=1)
    order = place_order(db, cart, buyer_name="Wanjiku", buyer_phone="254799888777")
    claim_payment(db, order, code)
    db.flush()
    return order


class TestTheSellerHome:
    def test_it_never_says_live_about_a_closed_shop(self, db: Session) -> None:
        """
        THE BUG THIS FILE STARTED FROM. The card read "Closed — buyers can't see
        it yet" directly above "8 live", which is true in the database and
        nonsense to a person. One word per idea.
        """
        seller = make_seller(db, whatsapp_number=PHONE)
        stocked(db, seller, count=8)

        replies = handle(db, PHONE, "hi").replies

        assert "Closed" in said(replies)
        assert "live" not in said(replies).lower()
        assert "8 items ready to sell" in said(replies)

    def test_money_comes_before_stock(self, db: Session) -> None:
        """A seller opening this thread wants to know who owes them money.
        Everything else is housekeeping, and housekeeping does not go first."""
        seller = make_seller(db, whatsapp_number=PHONE)
        make_payment_method(db, seller)
        an_order_awaiting(db, seller)
        make_product(db, seller, title="Unpriced", status=ProductStatus.DRAFT.value)

        body = said(handle(db, PHONE, "hi").replies)

        assert body.index("waiting for you to confirm") < body.index("a price")
        assert taps(handle(db, PHONE, "hi").replies)[0] == "orders"

    def test_there_is_always_something_to_tap(self, db: Session) -> None:
        """
        A seller with nothing outstanding still came here for something. An
        earlier version answered them with a status card carrying no buttons at
        all — a screenshot of a shop rather than a way into one.
        """
        seller = make_seller(db, whatsapp_number=PHONE, is_published=True)
        make_payment_method(db, seller)
        stocked(db, seller)

        assert taps(handle(db, PHONE, "hi").replies) == ["share"]

    def test_it_says_when_nobody_can_pay_them(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)
        stocked(db, seller)

        replies = handle(db, PHONE, "hi").replies

        assert "no way to pay you" in said(replies)
        assert "payments" in taps(replies)

    def test_an_unreadable_message_gets_their_own_shop_back(self, db: Session) -> None:
        """
        NOT "ARE YOU HERE TO SELL, OR TO SHOP?" — asked of somebody whose shop
        we are already running, that reads as the system forgetting them.
        """
        seller = make_seller(db, whatsapp_number=PHONE, is_published=True)
        make_payment_method(db, seller)
        stocked(db, seller)

        replies = handle(db, PHONE, "asdkjhasd").replies

        assert seller.display_name in said(replies)
        assert "here to sell" not in said(replies)


class TestGettingPaidIsSetUpInChat:
    """
    A seller who cannot be paid has a catalogue, not a shop — and sending them to
    a browser to fix that is the one hop this product exists to remove.
    """

    def test_it_offers_the_three_that_exist(self, db: Session) -> None:
        make_seller(db, whatsapp_number=PHONE)

        replies = handle(db, PHONE, "payments").replies

        assert taps(replies) == ["pay:pochi", "pay:till", "pay:paybill"]
        assert get_conversation(db, PHONE).state == ConversationState.PAY_KIND

    def test_pochi_takes_one_number_and_is_done(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)

        handle(db, PHONE, "payments")
        handle(db, PHONE, "pay:pochi")
        replies = handle(db, PHONE, "0712345678").replies

        method = db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id))
        assert method is not None
        assert method.kind == PaymentMethodKind.POCHI.value
        assert "Saved" in said(replies)
        assert get_conversation(db, PHONE).state == ConversationState.NEW

    def test_a_paybill_is_asked_for_its_account_too(self, db: Session) -> None:
        """Two answers to one question, held in the conversation between them —
        a paybill without an account reference is a payment nobody can trace."""
        seller = make_seller(db, whatsapp_number=PHONE)

        handle(db, PHONE, "payments")
        handle(db, PHONE, "pay:paybill")
        asked = handle(db, PHONE, "400200").replies

        assert "account number" in said(asked)

        handle(db, PHONE, "BOOKS01")

        method = db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id))
        assert method is not None
        assert method.number == "400200"
        assert method.account_reference == "BOOKS01"

    def test_a_bad_number_saves_nothing_and_asks_again(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)

        handle(db, PHONE, "payments")
        handle(db, PHONE, "pay:till")
        handle(db, PHONE, "not a number")

        assert db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id)) is None
        # Still in the state that can hear a second attempt, rather than dumped
        # back at the start of setup.
        assert get_conversation(db, PHONE).state == ConversationState.PAY_NUMBER


class TestOpeningIsGuarded:
    def test_a_shop_nobody_can_pay_does_not_open(self, db: Session) -> None:
        """
        THE GUARD THAT MATTERS MOST. Without it a buyer chooses, commits,
        reaches checkout and finds no way to send money — which costs the seller
        a customer they had already won.
        """
        seller = make_seller(db, whatsapp_number=PHONE)
        stocked(db, seller)

        replies = handle(db, PHONE, "open").replies

        assert seller.is_published is False
        assert "how do buyers pay you" in said(replies).lower()
        # And the refusal hands them the fix rather than just the wall.
        assert "payments" in taps(replies)

    def test_an_empty_shop_does_not_open(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)
        make_payment_method(db, seller)

        handle(db, PHONE, "open")

        assert seller.is_published is False

    def test_a_ready_shop_opens_and_hands_over_the_link(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The number has to be configured for a link to exist at all — see
        # TestTheShareLink for what happens when it is not.
        monkeypatch.setattr(get_settings(), "whatsapp_display_number", "254118198343")
        seller = make_seller(db, whatsapp_number=PHONE)
        make_payment_method(db, seller)
        stocked(db, seller)

        replies = handle(db, PHONE, "open").replies

        assert seller.is_published is True
        assert "open for business" in said(replies).lower()
        assert "wa.me" in said(replies)


class TestOrdersAndConfirmation:
    def test_orders_lists_what_is_waiting(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)
        make_payment_method(db, seller)
        order = an_order_awaiting(db, seller)

        replies = handle(db, PHONE, "orders").replies

        assert order.reference in said(replies)
        assert "Wanjiku" in said(replies)
        assert "SJ4K2L9X" in said(replies)
        assert f"confirm:{order.reference}" in taps(replies)

    def test_confirming_is_what_makes_an_order_paid(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)
        make_payment_method(db, seller)
        order = an_order_awaiting(db, seller)

        handle(db, PHONE, f"confirm:{order.reference}")

        db.refresh(order)
        assert order.status == OrderStatus.PAID.value
        assert order.paid_at is not None

    def test_another_seller_cannot_confirm_it(self, db: Session) -> None:
        """
        THE ONE THAT WOULD HURT. An order reference travels — it is in the
        buyer's receipt and in this thread. Without the ownership check, anybody
        holding one could close somebody else's order and tell a buyer they were
        square.
        """
        owner = make_seller(db, whatsapp_number=PHONE)
        make_payment_method(db, owner)
        order = an_order_awaiting(db, owner)

        intruder = make_seller(
            db, whatsapp_number=OTHER_PHONE, slug="zumafashion", display_name="Zuma Fashion"
        )
        make_payment_method(db, intruder)

        replies = handle(db, OTHER_PHONE, f"confirm:{order.reference}").replies

        db.refresh(order)
        assert order.status == OrderStatus.AWAITING_CONFIRMATION.value
        assert "can't find that order" in said(replies)

    def test_nothing_waiting_is_a_sentence_not_an_empty_list(self, db: Session) -> None:
        seller = make_seller(db, whatsapp_number=PHONE)
        make_payment_method(db, seller)
        stocked(db, seller)

        assert "Nothing waiting on you" in said(handle(db, PHONE, "orders").replies)


class TestTheShareLink:
    def test_it_is_a_wa_me_deep_link(self, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        NOT THE STOREFRONT URL. Tapping an http(s) link hands an intent to the
        operating system, which is free to open it in Chrome — and does. A wa.me
        link is not a page request, so nothing can route it out of WhatsApp.
        """
        monkeypatch.setattr(get_settings(), "whatsapp_display_number", "254118198343")
        seller = make_seller(
            db, whatsapp_number=PHONE, is_published=True, display_name="Book Lounge"
        )
        make_payment_method(db, seller)
        stocked(db, seller)

        body = said(handle(db, PHONE, "share").replies)

        assert "https://wa.me/254118198343?text=" in body
        assert "Book%20Lounge" in body

    def test_without_a_number_it_says_so_instead_of_inventing_one(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A seller posting a dead link to their status is worse than being told
        we cannot build one yet — and it is our fault, so the copy says so."""
        monkeypatch.setattr(get_settings(), "whatsapp_display_number", None)
        seller = make_seller(db, whatsapp_number=PHONE, is_published=True)
        make_payment_method(db, seller)
        stocked(db, seller)

        body = said(handle(db, PHONE, "share").replies)

        assert "wa.me" not in body
        assert "ours to fix" in body


class TestABuyerArriving:
    def test_the_link_names_the_shop_the_way_a_person_would(self, db: Session) -> None:
        """The buyer's own first message shows in their chat. It should read
        like something they wrote, not like a command they mistyped."""
        seller = make_seller(db, is_published=True, display_name="Book Lounge", slug="book-lounge")
        stocked(db, seller)

        replies = handle(db, "254700111222", "Shop Book Lounge").replies

        assert "Book Lounge" in said(replies)

    def test_the_old_slug_form_still_works(self, db: Session) -> None:
        """Links already in circulation cannot stop working because we improved
        the wording of new ones."""
        seller = make_seller(db, is_published=True, display_name="Book Lounge", slug="book-lounge")
        stocked(db, seller)

        replies = handle(db, "254700111222", "shop book-lounge").replies

        assert "Book Lounge" in said(replies)

    def test_a_closed_shop_is_not_reachable_by_name(self, db: Session) -> None:
        """The publish gate, from the other side. A chat that ignored it would
        be a way around it."""
        make_seller(db, is_published=False, display_name="Book Lounge", slug="book-lounge")

        replies = handle(db, "254700111222", "Shop Book Lounge").replies

        assert "can't find" in said(replies)
