"""
The three moments where a conversation concerns somebody who is not typing.

WHAT WAS BROKEN. ``notify_paid`` was called from exactly one place — the Daraja
STK callback. So the loop closed only for shops with their own Daraja
credentials, and every Pochi seller, which is most of them, had a silent one:

    a buyer placed an order and the seller was never told
    a buyer sent their M-Pesa code and the seller was never told
    the seller confirmed and the BUYER was never told

The third is the one that mattered most, because the chat had already made the
promise out loud. ``_claim`` says, in writing:

    "You'll get a message here when they do."

Nothing delivered it. The buyer watched a thread go quiet on the only question
they had.

WHY THESE ARE TESTED ON `Outcome.notify` RATHER THAN ON A SENT MESSAGE. The bot
never sends; it returns what should be sent, and the webhook does the sending
after the commit. That seam is deliberate — an alert must not go out for an
order that then failed to save — so the assertion belongs on the intent, and the
webhook's own test covers the dispatch.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, OrderStatus, Product, ProductStatus, Seller
from app.services.bot import handle
from tests.factories import make_payment_method, make_product, make_seller

SELLER_PHONE = "254712345678"
BUYER_PHONE = "254700111222"


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
        title="Leather Tote Bag",
        price_kes=2800,
        status=ProductStatus.PUBLISHED.value,
        stock=5,
    )
    return seller


def order_placed(db: Session, seller: Seller) -> Order:
    """Walk a real buyer to a real order, through the real conversation."""
    product = db.scalar(select(Product))
    assert product is not None
    handle(db, BUYER_PHONE, "Shop Vitabu Bora")
    handle(db, BUYER_PHONE, f"prod:{product.id}")
    handle(db, BUYER_PHONE, "add")
    handle(db, BUYER_PHONE, "checkout")
    handle(db, BUYER_PHONE, "Akinyi Otieno")
    handle(db, BUYER_PHONE, "collect")
    order = db.scalar(select(Order))
    assert order is not None
    return order


class TestTheSellerHearsAboutAnOrder:
    def test_placing_an_order_alerts_the_seller(self, db: Session) -> None:
        """
        AT PLACEMENT, NOT AT PAYMENT. A seller who only hears once the money has
        settled cannot set anything aside, and the buyer is waiting to be told
        it is coming.
        """
        a_shop(db)
        product = db.scalar(select(Product))
        assert product is not None

        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        handle(db, BUYER_PHONE, f"prod:{product.id}")
        handle(db, BUYER_PHONE, "add")
        handle(db, BUYER_PHONE, "checkout")
        handle(db, BUYER_PHONE, "Akinyi Otieno")
        outcome = handle(db, BUYER_PHONE, "collect")

        assert len(outcome.notify) == 1
        to, reply = outcome.notify[0]
        assert to == SELLER_PHONE
        assert "New order" in reply.body
        assert "Akinyi Otieno" in reply.body
        assert BUYER_PHONE in reply.body

    def test_it_does_not_tell_the_seller_they_have_been_paid(self, db: Session) -> None:
        """
        THE ONE LIE THAT WOULD COST REAL MONEY. Nothing has arrived yet. A seller
        told they are paid hands over the goods.
        """
        a_shop(db)
        product = db.scalar(select(Product))
        assert product is not None

        handle(db, BUYER_PHONE, "Shop Vitabu Bora")
        handle(db, BUYER_PHONE, f"prod:{product.id}")
        handle(db, BUYER_PHONE, "add")
        handle(db, BUYER_PHONE, "checkout")
        handle(db, BUYER_PHONE, "Akinyi Otieno")
        outcome = handle(db, BUYER_PHONE, "collect")

        body = outcome.notify[0][1].body
        assert "paid" not in body.lower()


class TestTheSellerHearsAboutAClaim:
    def test_a_code_alerts_the_seller_with_the_confirm_button(self, db: Session) -> None:
        seller = a_shop(db)
        order = order_placed(db, seller)

        outcome = handle(db, BUYER_PHONE, "SLK7XA2B9C")

        assert len(outcome.notify) == 1
        to, reply = outcome.notify[0]
        assert to == SELLER_PHONE
        assert "SLK7XA2B9C" in reply.body
        assert "says they've paid" in reply.body
        # The alert carries the action, so the seller never has to go and find
        # the order it is about.
        assert reply.buttons == [(f"confirm:{order.reference}", "Confirm paid")]

    def test_the_alert_says_what_the_buyer_claims_not_what_is_true(self, db: Session) -> None:
        """
        A code is text the buyer typed. It looks identical whether it came off a
        real M-Pesa message or was invented, so the wording has to stay a report
        of what was said.
        """
        seller = a_shop(db)
        order_placed(db, seller)

        body = handle(db, BUYER_PHONE, "SLK7XA2B9C").notify[0][1].body

        assert "says they've paid" in body
        assert "Check your M-Pesa" in body

    def test_a_rejected_code_alerts_nobody(self, db: Session) -> None:
        """Nothing was recorded, so there is nothing to tell anyone about."""
        seller = a_shop(db)
        order_placed(db, seller)

        outcome = handle(db, BUYER_PHONE, "that's not a code")

        assert outcome.notify == []


class TestTheBuyerHearsBack:
    def test_confirming_sends_the_buyer_their_receipt(self, db: Session) -> None:
        """
        THE PROMISE THE CHAT ALREADY MADE. "You'll get a message here when they
        do" was in the copy with nothing behind it.
        """
        seller = a_shop(db)
        order = order_placed(db, seller)
        handle(db, BUYER_PHONE, "SLK7XA2B9C")

        outcome = handle(db, SELLER_PHONE, f"confirm:{order.reference}")

        assert len(outcome.notify) == 1
        to, reply = outcome.notify[0]
        assert to == order.buyer_phone
        assert "Payment received" in reply.body
        assert order.reference in reply.body
        assert "Vitabu Bora" in reply.body

    def test_the_seller_is_told_the_buyer_was_told(self, db: Session) -> None:
        """Otherwise the seller sends their own "it's confirmed" message, and the
        buyer gets it twice from two people."""
        seller = a_shop(db)
        order = order_placed(db, seller)
        handle(db, BUYER_PHONE, "SLK7XA2B9C")

        outcome = handle(db, SELLER_PHONE, f"confirm:{order.reference}")

        assert "I've told Akinyi Otieno" in outcome.replies[0].body

    def test_a_failed_confirmation_notifies_nobody(self, db: Session) -> None:
        """An order that was already closed did not change, so no one is told it
        did."""
        seller = a_shop(db)
        order = order_placed(db, seller)
        handle(db, BUYER_PHONE, "SLK7XA2B9C")
        handle(db, SELLER_PHONE, f"confirm:{order.reference}")

        outcome = handle(db, SELLER_PHONE, f"confirm:{order.reference}")

        db.refresh(order)
        assert order.status == OrderStatus.PAID.value
        assert outcome.notify == []

    def test_another_sellers_confirmation_notifies_nobody(self, db: Session) -> None:
        """The ownership check already refuses it; this proves the refusal does
        not still fire a receipt at somebody else's buyer."""
        seller = a_shop(db)
        order = order_placed(db, seller)
        handle(db, BUYER_PHONE, "SLK7XA2B9C")

        intruder = make_seller(db, whatsapp_number="254799888000", slug="zuma", display_name="Zuma")
        make_payment_method(db, intruder)

        outcome = handle(db, "254799888000", f"confirm:{order.reference}")

        db.refresh(order)
        assert order.status == OrderStatus.AWAITING_CONFIRMATION.value
        assert outcome.notify == []
