"""
The STK path: the push, and believing the callback exactly once.

**No test here touches Safaricom.** ``FakeStkEngine`` satisfies the ``StkEngine``
Protocol, which is the entire reason that seam exists.

The thing being defended is idempotency. Daraja retries — on timeout, on a slow
response, and sometimes for no visible reason — and a retry that applied twice
would settle one purchase as two payments. Two mechanisms make that impossible
and both are tested here:

    checkout_request_id is UNIQUE      a duplicate row cannot exist
    apply_callback returns early       a replay changes nothing

The forged-callback cases matter as much. The endpoint is public and Safaricom
does not sign its callbacks, so the only things standing between a stranger and
a free order are: we accept ids we issued, and we check the amount.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    OrderStatus,
    PaymentMethod,
    PaymentMethodKind,
    ProductStatus,
    Seller,
)
from app.secrets_vault import encrypt
from app.services.cart import add_item, get_or_create_cart
from app.services.daraja import DarajaError, StkPushResult, normalise_phone
from app.services.orders import place_order
from app.services.payments import PaymentError, apply_callback, start_stk_payment
from tests.factories import make_payment_method, make_product, make_seller

CHECKOUT_ID = "ws_CO_22082026_000001"


class FakeStkEngine:
    """Satisfies StkEngine. Records calls; never leaves the process."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def push(
        self,
        method: Any,
        *,
        amount_kes: int,
        phone: str,
        reference: str,
        description: str,
        callback_url: str,
    ) -> StkPushResult:
        if self.fail:
            raise DarajaError(self.fail)
        self.calls.append(
            {
                "amount_kes": amount_kes,
                "phone": phone,
                "reference": reference,
                "callback_url": callback_url,
            }
        )
        return StkPushResult(
            checkout_request_id=CHECKOUT_ID,
            merchant_request_id="29115-34620561-1",
            customer_message="Success. Request accepted for processing",
        )


def stk_shop(db: Session, **overrides: Any) -> Seller:
    """A published shop with a Till and full Daraja credentials."""
    seller = make_seller(db, **overrides)
    seller.is_published = True
    db.flush()
    make_payment_method(
        db,
        seller,
        kind=PaymentMethodKind.TILL.value,
        number="832145",
        stk_shortcode="174379",
        consumer_key_enc=encrypt("key"),
        consumer_secret_enc=encrypt("secret"),
        passkey_enc=encrypt("passkey"),
    )
    return seller


def method_for(seller: Seller) -> PaymentMethod:
    """
    The shop's payment method, asserted present.

    ``stk_shop`` always creates one, but the relationship is Optional
    because most shops legitimately have none until the seller sets it up.
    """
    method = seller.payment_method
    assert method is not None
    return method


def an_order(db: Session, seller: Seller, price_kes: int = 1500) -> Any:
    """A placed order worth ``price_kes``."""
    product = make_product(
        db, seller, price_kes=price_kes, stock=5, status=ProductStatus.PUBLISHED.value
    )
    cart = get_or_create_cart(db, None, seller)
    add_item(db, cart, product.id)
    return place_order(db, cart, buyer_name="Amina", buyer_phone="0712345678")


def callback_for(
    order_total: int,
    *,
    result_code: int = 0,
    receipt: str | None = "SLK7XA2B9C",
    checkout_id: str = CHECKOUT_ID,
) -> dict[str, Any]:
    """Daraja's callback shape, as it really arrives."""
    body: dict[str, Any] = {
        "Body": {
            "stkCallback": {
                "MerchantRequestID": "29115-34620561-1",
                "CheckoutRequestID": checkout_id,
                "ResultCode": result_code,
                "ResultDesc": "The service request is processed successfully."
                if result_code == 0
                else "Request cancelled by user",
            }
        }
    }
    if result_code == 0:
        items: list[dict[str, Any]] = [{"Name": "Amount", "Value": order_total}]
        if receipt:
            items.append({"Name": "MpesaReceiptNumber", "Value": receipt})
        body["Body"]["stkCallback"]["CallbackMetadata"] = {"Item": items}
    return body


class TestPhoneNormalisation:
    @pytest.mark.parametrize(
        "raw",
        ["0712345678", "+254712345678", "254712345678", "712345678", "0712 345 678"],
    )
    def test_every_form_a_buyer_types_is_accepted(self, raw: str) -> None:
        """
        Daraja accepts exactly one shape and rejects the rest with an error that
        does not say so. The buyer must never be corrected about how to write
        their own phone number.
        """
        assert normalise_phone(raw) == "254712345678"

    @pytest.mark.parametrize("raw", ["12345", "abc", ""])
    def test_nonsense_is_refused(self, raw: str) -> None:
        with pytest.raises(DarajaError):
            normalise_phone(raw)


class TestStartingAPush:
    def test_a_push_records_the_checkout_id(self, db: Session) -> None:
        seller = stk_shop(db)
        order = an_order(db, seller)
        engine = FakeStkEngine()

        payment = start_stk_payment(db, order, method_for(seller), engine, "https://x/cb")

        assert payment.checkout_request_id == CHECKOUT_ID
        assert payment.confirmed_at is None
        assert order.status == OrderStatus.PENDING.value

    def test_a_push_is_not_a_payment(self, db: Session) -> None:
        """
        The prompt was accepted for delivery. The buyer may still decline it,
        mistype their PIN, or never see it.
        """
        seller = stk_shop(db)
        order = an_order(db, seller)

        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")

        assert order.paid_at is None
        assert order.status != OrderStatus.PAID.value

    def test_a_refused_push_writes_no_row(self, db: Session) -> None:
        """
        A payment row with no checkout id records nothing, and would occupy the
        unique constraint that makes replays safe.
        """
        seller = stk_shop(db)
        order = an_order(db, seller)

        with pytest.raises(PaymentError):
            start_stk_payment(
                db,
                order,
                method_for(seller),
                FakeStkEngine(fail="Invalid shortcode"),
                "https://x/cb",
            )

        assert order.payments == []


class TestTheCallbackIsTheOnlyTruth:
    def test_a_success_callback_pays_the_order(self, db: Session) -> None:
        seller = stk_shop(db)
        order = an_order(db, seller)
        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")

        apply_callback(db, callback_for(order.total_kes))

        assert order.status == OrderStatus.PAID.value
        assert order.paid_at is not None
        assert order.payments[-1].mpesa_receipt == "SLK7XA2B9C"

    def test_replaying_the_same_callback_changes_nothing(self, db: Session) -> None:
        """
        THE ONE THAT MATTERS MOST. Daraja retries, and a retry applied twice
        would settle one purchase as two payments.
        """
        seller = stk_shop(db)
        order = an_order(db, seller)
        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")
        payload = callback_for(order.total_kes)

        apply_callback(db, payload)
        paid_at = order.paid_at

        for _ in range(5):
            apply_callback(db, copy.deepcopy(payload))

        assert len(order.payments) == 1
        assert order.paid_at == paid_at
        assert order.status == OrderStatus.PAID.value

    def test_a_declined_payment_returns_the_order_to_pending(self, db: Session) -> None:
        """
        A failed attempt is not a failed order. Locking the buyer out of trying
        again loses a sale that was nearly made.
        """
        seller = stk_shop(db)
        order = an_order(db, seller)
        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")

        apply_callback(db, callback_for(order.total_kes, result_code=1032))

        assert order.status == OrderStatus.PENDING.value
        assert order.paid_at is None
        assert order.payments[-1].result_code == 1032

    def test_an_unknown_checkout_id_is_ignored(self, db: Session) -> None:
        """
        Anyone can POST to the callback. Accepting an id we never issued would
        let a stranger manufacture payments.
        """
        seller = stk_shop(db)
        order = an_order(db, seller)

        result = apply_callback(db, callback_for(order.total_kes, checkout_id="ws_CO_FORGED"))

        assert result is None
        assert order.status == OrderStatus.PENDING.value

    def test_an_underpayment_does_not_settle_the_order(self, db: Session) -> None:
        """A forged callback must not settle an order for less than it costs."""
        seller = stk_shop(db)
        order = an_order(db, seller, price_kes=5000)
        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")

        apply_callback(db, callback_for(1))

        assert order.status != OrderStatus.PAID.value
        assert order.payments[-1].confirmed_at is None
        assert "mismatch" in (order.payments[-1].result_desc or "").lower()

    def test_success_with_no_receipt_is_not_a_payment(self, db: Session) -> None:
        """
        ``ck_payments_confirmed_has_receipt`` would refuse the row anyway;
        failing here says why instead of surfacing a constraint violation.
        """
        seller = stk_shop(db)
        order = an_order(db, seller)
        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")

        apply_callback(db, callback_for(order.total_kes, receipt=None))

        assert order.status != OrderStatus.PAID.value
        assert order.payments[-1].confirmed_at is None

    def test_the_raw_callback_is_kept_verbatim(self, db: Session) -> None:
        """When a payment is disputed this is the only evidence; a parsed
        summary is an opinion about it."""
        seller = stk_shop(db)
        order = an_order(db, seller)
        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")
        payload = callback_for(order.total_kes)

        apply_callback(db, payload)

        assert order.payments[-1].raw_callback == payload


class TestTheCallbackEndpoint:
    def test_it_acknowledges_with_daraja_s_shape(self, client: TestClient, db: Session) -> None:
        seller = stk_shop(db)
        order = an_order(db, seller)
        start_stk_payment(db, order, method_for(seller), FakeStkEngine(), "https://x/cb")

        response = client.post("/payments/mpesa/callback", json=callback_for(order.total_kes))

        assert response.status_code == 200
        assert response.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}

    def test_an_unknown_id_still_gets_a_200(self, client: TestClient, db: Session) -> None:
        """
        Returning an error would only make Safaricom retry something that can
        never succeed.
        """
        response = client.post(
            "/payments/mpesa/callback", json=callback_for(100, checkout_id="ws_CO_NOPE")
        )
        assert response.status_code == 200

    def test_a_pochi_shop_cannot_request_a_prompt(self, client: TestClient, db: Session) -> None:
        """
        Daraja cannot push to Pochi la Biashara. The buyer is sent back to pay
        the number by hand, which is a working outcome rather than an error.
        """
        seller = make_seller(db, slug="pochishop")
        seller.is_published = True
        db.flush()
        make_payment_method(db, seller)
        order = an_order(db, seller)

        response = client.post(
            f"/shop/{seller.slug}/order/{order.reference}/pay", follow_redirects=False
        )

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
