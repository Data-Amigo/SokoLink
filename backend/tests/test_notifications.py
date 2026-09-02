"""
Telling both sides that money moved.

Three rules govern every message here, and none of them is obvious from a call
site — which is why they live in one service and are pinned here:

    consent      the buyer is messaged only if they ticked the box
    the window   Meta delivers free-form messages only inside 24 hours, so a
                 quiet seller is unreachable and that is EXPECTED
    never fatal  a message that will not send must not undo a payment

The third is the one that would do real damage if it broke. The callback is the
only automatic payment truth; a notifier that could fail it would have Safaricom
redeliver a payment already applied.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import Order, OrderStatus, ProductStatus
from app.services.cart import add_item, get_or_create_cart
from app.services.messaging import MessagingError
from app.services.notifications import notify_paid
from app.services.orders import place_order
from tests.factories import make_payment_method, make_product, make_seller

BUYER = "254712345678"
SELLER_PHONE = "254733444555"


class FakeMessenger:
    """Records what would have been sent. Nothing leaves the process."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._fail_for = fail_for or set()

    def send(self, to: str, body: str) -> str:
        if to in self._fail_for:
            raise MessagingError("Re-engagement message outside 24 hour window")
        self.sent.append((to, body))
        return "wamid.fake"

    def to(self, number: str) -> str:
        """Everything sent to one number, joined."""
        return "\n".join(body for dest, body in self.sent if dest == number)


def paid_order(db: Session, *, opt_in: bool = True, **seller_kwargs: Any) -> Order:
    """One paid order, built the way a real one is."""
    seller = make_seller(db, whatsapp_number=SELLER_PHONE, **seller_kwargs)
    seller.is_published = True
    make_payment_method(db, seller)
    product = make_product(
        db,
        seller,
        title="Ankara Shirt",
        price_kes=1500,
        stock=5,
        status=ProductStatus.PUBLISHED.value,
    )
    cart = get_or_create_cart(db, None, seller)
    add_item(db, cart, product.id, quantity=2)
    order = place_order(
        db,
        cart=cart,
        buyer_name="Akinyi Otieno",
        buyer_phone=BUYER,
        delivery_address="Kasarani",
        whatsapp_opt_in=opt_in,
    )
    order.status = OrderStatus.PAID.value
    from datetime import UTC, datetime

    order.paid_at = datetime.now(UTC)
    db.flush()
    return order


class TestTheBuyerReceipt:
    def test_a_buyer_who_opted_in_gets_one(self, db: Session) -> None:
        order = paid_order(db)
        messenger = FakeMessenger()

        reached = notify_paid(db, order, messenger)

        assert "buyer" in reached
        assert "Payment received" in messenger.to(BUYER)

    def test_it_names_the_order_and_the_amount(self, db: Session) -> None:
        """
        This is the message a buyer scrolls back to when something goes wrong.
        A receipt that does not identify its order is not a receipt.
        """
        order = paid_order(db)
        messenger = FakeMessenger()

        notify_paid(db, order, messenger)

        receipt = messenger.to(BUYER)
        assert order.reference in receipt
        assert "KSh 3,000" in receipt
        assert "Ankara Shirt" in receipt

    def test_a_buyer_who_did_not_opt_in_is_left_alone(self, db: Session) -> None:
        """
        CONSENT IS THE BUYER'S ANSWER, NOT OUR CONVENIENCE. The webview does not
        know who is looking at it — a link opened from a status is just a
        browser tab — so the phone we hold was typed for M-Pesa. Using it for
        anything else without the tick is helping ourselves to it.
        """
        order = paid_order(db, opt_in=False)
        messenger = FakeMessenger()

        reached = notify_paid(db, order, messenger)

        assert "buyer" not in reached
        assert messenger.to(BUYER) == ""

    def test_the_seller_is_still_told_when_the_buyer_opted_out(self, db: Session) -> None:
        """One person declining a message must not cost the other theirs."""
        order = paid_order(db, opt_in=False)
        messenger = FakeMessenger()

        reached = notify_paid(db, order, messenger)

        assert reached == ["seller"]


class TestTheSellerAlert:
    def test_the_seller_learns_they_have_a_sale(self, db: Session) -> None:
        order = paid_order(db)
        messenger = FakeMessenger()

        assert "seller" in notify_paid(db, order, messenger)
        assert "You have a sale" in messenger.to(SELLER_PHONE)

    def test_it_carries_the_buyer_s_number_and_address(self, db: Session) -> None:
        """
        A seller's next action is to arrange delivery. Making them open a
        browser to find a number they were just told about is exactly the
        friction this product exists to remove.
        """
        order = paid_order(db)
        messenger = FakeMessenger()

        notify_paid(db, order, messenger)

        alert = messenger.to(SELLER_PHONE)
        assert BUYER in alert
        assert "Akinyi Otieno" in alert
        assert "Kasarani" in alert

    def test_an_unreachable_seller_is_not_an_error(self, db: Session) -> None:
        """
        THE EXPECTED CASE, NOT A BROKEN ONE. Meta delivers free-form messages
        only inside the 24-hour window a person opens by messaging us. A seller
        quiet for a week is outside it and their alert fails until an approved
        template exists. They still see the sale in the workspace.
        """
        order = paid_order(db)
        messenger = FakeMessenger(fail_for={SELLER_PHONE})

        reached = notify_paid(db, order, messenger)

        assert reached == ["buyer"]

    def test_a_shop_with_no_number_is_skipped(self, db: Session) -> None:
        """
        Reachable, but only by a specific route, and the database enforces the
        order of it: ck_sellers_published_needs_whatsapp refuses to remove the
        number while the shop is open. So the seller closes the shop, drops the
        number, and an OLD paid order still needs notifying. Closing first is
        not test scaffolding — it is the only sequence Postgres permits.
        """
        order = paid_order(db)
        order.seller.is_published = False
        db.flush()
        order.seller.whatsapp_number = None
        db.flush()
        messenger = FakeMessenger()

        assert notify_paid(db, order, messenger) == ["buyer"]


class TestItNeverBreaksAPayment:
    def test_both_failing_still_returns_cleanly(self, db: Session) -> None:
        """
        THE RULE THAT MATTERS MOST. The money has arrived and the order is
        recorded. If this raised, the callback would return a non-200 and
        Safaricom would redeliver a payment already applied.
        """
        order = paid_order(db)
        messenger = FakeMessenger(fail_for={BUYER, SELLER_PHONE})

        assert notify_paid(db, order, messenger) == []

    def test_it_changes_nothing_about_the_order(self, db: Session) -> None:
        """
        A notifier that could alter state would be a second source of truth
        about money. It reads an order that is already paid and says so.
        """
        order = paid_order(db)
        before = (order.status, order.total_kes, order.paid_at)

        notify_paid(db, order, FakeMessenger())

        assert (order.status, order.total_kes, order.paid_at) == before


class TestChoosingAProvider:
    def test_meta_is_preferred_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        A deployment that has configured the Cloud API gets the CloudMessenger,
        which is now the only provider.
        """
        from app.config import get_settings
        from app.services.messaging import CloudMessenger, get_messenger

        settings = get_settings()
        monkeypatch.setattr(settings, "whatsapp_access_token", "tok")
        monkeypatch.setattr(settings, "whatsapp_phone_number_id", "123")

        assert isinstance(get_messenger(), CloudMessenger)

    def test_neither_configured_names_both_ways_to_fix_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.config import get_settings
        from app.services.messaging import get_messenger

        settings = get_settings()
        for name in (
            "whatsapp_access_token",
            "whatsapp_phone_number_id",
        ):
            monkeypatch.setattr(settings, name, None)

        with pytest.raises(MessagingError) as caught:
            get_messenger()

        assert "WHATSAPP_ACCESS_TOKEN" in str(caught.value)
        assert "WHATSAPP_PHONE_NUMBER_ID" in str(caught.value)
