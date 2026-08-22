"""
The STK path: asking for money, and believing the answer exactly once.

    start_stk_payment() ──▶ Payment(checkout_request_id) ──▶ handset prompt
                                        │
                                        ▼
    Daraja POSTs the result ──▶ apply_callback() ──▶ Order(paid | failed)
                                        │
                                  replayed? no-op

**THE CALLBACK IS THE ONLY PAYMENT TRUTH ON THIS PATH.** A successful ``push()``
means a prompt was accepted for delivery and nothing more — the buyer may still
decline it, mistype their PIN, run out of balance, or never see it. Nothing but
this callback may mark an STK order paid.

IDEMPOTENCY IS THE WHOLE DESIGN, because Daraja retries. It retries on timeout,
on a slow response, and sometimes for no visible reason, and a retry that
applied twice would settle one purchase as two payments. Two things make that
impossible:

  1. ``checkout_request_id`` is UNIQUE in the database, so a duplicate row
     cannot exist even if two callbacks arrive at the same instant.
  2. ``apply_callback`` returns early when the payment is already confirmed,
     so a replay changes nothing and still answers 200 — because an error
     response would only make Safaricom retry harder.

THIS ENDPOINT IS PUBLIC AND UNSIGNED. Safaricom does not sign callbacks; there
is no HMAC to check. What protects it instead:

  - ``CheckoutRequestID`` is issued by Daraja and unguessable, and we accept
    only ids we have actually issued — an unknown id is ignored, not created.
  - The amount is compared with the order's own total, so a forged callback
    cannot settle an order for less than it costs.

That is weaker than a signature and is stated plainly rather than glossed. It is
also why the manual path is not the poor relation here: a seller reading their
own M-Pesa inbox is, in a real sense, the stronger check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, OrderStatus, Payment, PaymentMethod
from app.services.daraja import DarajaError, StkEngine, StkPushResult

#: Daraja's success code. Anything else is a failure with a reason attached.
RESULT_OK = 0


class PaymentError(RuntimeError):
    """A payment could not be started, with a message safe to show a buyer."""


def start_stk_payment(
    db: Session,
    order: Order,
    method: PaymentMethod,
    engine: StkEngine,
    callback_url: str,
) -> Payment:
    """
    Prompt the buyer's handset and record the attempt.

    Args:
        db: Session.
        order: The order being paid. Must not be final.
        method: The seller's STK-capable payment configuration.
        engine: The Daraja seam. A fake in tests; no test calls Safaricom.
        callback_url: Publicly reachable URL Daraja will POST the result to.

    Returns:
        The ``Payment`` row holding the ``checkout_request_id`` the callback
        will arrive under.

    Raises:
        PaymentError: If the order is closed, or Daraja refuses the push. The
            row is written only AFTER Daraja accepts — a payment row with no
            checkout id is a record of nothing, and it would occupy the unique
            constraint that makes replays safe.
    """
    if order.status_enum.is_final:
        raise PaymentError("This order is already closed.")

    try:
        result: StkPushResult = engine.push(
            method,
            amount_kes=order.total_kes,
            phone=order.buyer_phone,
            reference=order.reference,
            description=f"Order {order.reference}",
            callback_url=callback_url,
        )
    except DarajaError as exc:
        raise PaymentError(str(exc)) from exc

    payment = Payment(
        order_id=order.id,
        method_kind=order.paid_to_kind,
        amount_kes=order.total_kes,
        phone=order.buyer_phone,
        checkout_request_id=result.checkout_request_id,
        merchant_request_id=result.merchant_request_id,
    )
    db.add(payment)
    db.flush()
    return payment


def _extract(callback: dict[str, Any]) -> dict[str, Any]:
    """
    Pull the fields we need out of Daraja's nested callback shape.

    The metadata arrives as a list of ``{"Name": ..., "Value": ...}`` pairs
    rather than an object, and is absent entirely on failure. Flattening it here
    keeps that shape in one place instead of spread through the caller.
    """
    body = callback.get("Body", {}).get("stkCallback", {})
    items = (body.get("CallbackMetadata") or {}).get("Item") or []
    metadata = {item.get("Name"): item.get("Value") for item in items if item.get("Name")}

    return {
        "checkout_request_id": body.get("CheckoutRequestID"),
        "merchant_request_id": body.get("MerchantRequestID"),
        "result_code": body.get("ResultCode"),
        "result_desc": body.get("ResultDesc"),
        "receipt": metadata.get("MpesaReceiptNumber"),
        "amount": metadata.get("Amount"),
    }


def apply_callback(db: Session, callback: dict[str, Any]) -> Payment | None:
    """
    Apply Daraja's verdict to an order, exactly once.

    Args:
        db: Session.
        callback: The raw JSON body Safaricom posted, unmodified.

    Returns:
        The updated ``Payment``, or None when the callback names a
        ``CheckoutRequestID`` we never issued.

    Notes:
        **An unknown id is ignored rather than creating a row.** Anyone can POST
        to this endpoint; accepting an id we did not issue would let a stranger
        manufacture payments.

        **A replay is a no-op that still succeeds.** Returning an error would
        make Safaricom retry harder, and the second delivery of a payment we
        have already applied is not a problem to be reported — it is the normal
        behaviour of an at-least-once system.
    """
    fields = _extract(callback)
    checkout_id = fields["checkout_request_id"]
    if not checkout_id:
        return None

    payment = db.scalar(select(Payment).where(Payment.checkout_request_id == str(checkout_id)))
    if payment is None:
        return None

    # THE REPLAY GUARD. Everything below this line runs at most once per payment.
    if payment.confirmed_at is not None:
        return payment

    payment.raw_callback = callback
    payment.result_code = int(fields["result_code"]) if fields["result_code"] is not None else None
    payment.result_desc = fields["result_desc"]

    order = payment.order

    if payment.result_code != RESULT_OK:
        # A decline, a timeout, or a wrong PIN. The order returns to PENDING so
        # the buyer can try again — a failed attempt is not a failed order, and
        # locking them out of retrying would lose a sale that was nearly made.
        if not order.status_enum.is_final:
            order.status = OrderStatus.PENDING.value
        db.flush()
        return payment

    # Guard against a forged callback settling an order for less than it costs.
    # Daraja sends the amount it actually took; if that disagrees with what we
    # asked for, we do not have this order's money.
    paid_amount = fields["amount"]
    if paid_amount is not None and int(paid_amount) < order.total_kes:
        payment.result_desc = (
            f"Amount mismatch: received {int(paid_amount)}, expected {order.total_kes}"
        )
        db.flush()
        return payment

    now = datetime.now(UTC)
    payment.mpesa_receipt = str(fields["receipt"]) if fields["receipt"] else None
    if payment.mpesa_receipt is None:
        # ``ck_payments_confirmed_has_receipt`` would refuse this row anyway.
        # Failing here says why, instead of surfacing a constraint violation.
        payment.result_desc = "Daraja reported success with no receipt number."
        db.flush()
        return payment

    payment.confirmed_at = now
    order.status = OrderStatus.PAID.value
    order.paid_at = now
    db.flush()
    return payment
