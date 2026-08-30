"""
The M-Pesa callback, and starting an STK push.

    POST /payments/mpesa/callback     Safaricom posts here. PUBLIC.
    POST /shop/{slug}/order/{ref}/pay the buyer asks for a prompt

THIS ROUTE IS PUBLIC AND UNAUTHENTICATED, because Safaricom will not
authenticate to us. There is no signature to verify — Daraja does not sign
callbacks. What stands in for one is documented in ``services/payments.py``:
we accept only ``CheckoutRequestID`` values we ourselves issued, and we compare
the amount against the order's own total.

WHY IT ALWAYS ANSWERS 200 ON A PROCESSED CALLBACK, INCLUDING A REPLAY. Safaricom
retries anything that is not acknowledged. A replay is not an error — it is the
normal behaviour of an at-least-once delivery system, and reporting it as a
failure only makes the retries continue. The one case that DOES return an error
is an internal failure, where a retry is exactly what we want.

WHY IT NEVER ECHOES ANYTHING BACK. The response body is a fixed acknowledgement.
An endpoint that reflected the payload would be a free oracle for anyone probing
which order references exist.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.config import get_settings
from app.db import get_db
from app.models import OrderStatus
from app.services.daraja import StkEngine, get_stk_engine
from app.services.notifications import notify_paid
from app.services.orders import get_order, get_payment_method
from app.services.payments import (
    MPESA_CALLBACK_PATH,
    PaymentError,
    apply_callback,
    start_stk_payment,
)
from app.services.storefront import get_public_shop

logger = logging.getLogger(__name__)

router = APIRouter(tags=["payments"])

#: Where Daraja posts its verdict. Fixed, because it is registered with
#: Safaricom and changing it silently would strand every in-flight payment.
#: Re-exported from the service, which owns it — the chat checkout needs the
#: same path and cannot import it from a route.
CALLBACK_PATH = MPESA_CALLBACK_PATH


@router.post(CALLBACK_PATH)
async def mpesa_callback(request: Request, db: Session = Depends(get_db)) -> Response:
    """
    Daraja's verdict on one STK push. The only payment truth on that path.

    Returns:
        A fixed acknowledgement with HTTP 200 once the callback has been
        processed — including when it names an id we never issued, since
        retrying that would achieve nothing.

    Raises:
        HTTPException: 500 if the body cannot be read at all, so Safaricom
            retries. This is the one failure a retry can fix.
    """
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:  # noqa: BLE001 — any parse failure means "retry me"
        raise HTTPException(status_code=500, detail="Unreadable callback body") from exc

    payment = apply_callback(db, payload)

    # Daraja expects this exact shape. It means "received", not "paid".
    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    # AFTER THE COMMIT, AND NEVER ALLOWED TO FAIL IT. The money has arrived and
    # the order is recorded; a message that will not send must not turn that
    # into a non-200, because Safaricom would redeliver a payment we have
    # already applied. Both sides can still see the truth without this — the
    # seller in their workspace, the buyer in their own M-Pesa message.
    if payment is not None and payment.order.status_enum is OrderStatus.PAID:
        try:
            notify_paid(db, payment.order)
        except Exception:  # noqa: BLE001 — a notifier must never fail a payment
            logger.exception("could not notify anyone about %s", payment.order.reference)

    return JSONResponse({"ResultCode": 0, "ResultDesc": "Accepted"})


@router.post("/shop/{slug}/order/{reference}/pay")
def request_stk(
    slug: str,
    reference: str,
    db: Session = Depends(get_db),
    engine: StkEngine = Depends(get_stk_engine),
) -> Response:
    """
    Ask the buyer's handset for the money.

    Only offered when the seller is STK-capable — a Pochi shop can never reach
    here, and the order page does not show the button. A failure redirects back
    with the reason rather than erroring: the manual path is still open, and
    telling the buyer to pay the number by hand is a working outcome.

    Raises:
        HTTPException: 404 when the order is unknown or belongs to another shop.
    """
    seller = get_public_shop(db, slug)
    if seller is None:
        raise HTTPException(status_code=404, detail="Shop not found")

    order = get_order(db, reference)
    if order is None or order.seller_id != seller.id:
        raise HTTPException(status_code=404, detail="Order not found")

    method = get_payment_method(db, seller)
    if method is None or not method.can_stk:
        return RedirectResponse(
            url=f"/shop/{slug}/order/{reference}?error=Pay the number shown above.",
            status_code=303,
        )

    callback_url = f"{get_settings().app_base_url}{CALLBACK_PATH}"

    try:
        start_stk_payment(db, order, method, engine, callback_url)
    except PaymentError as exc:
        return RedirectResponse(url=f"/shop/{slug}/order/{reference}?error={exc}", status_code=303)

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return RedirectResponse(url=f"/shop/{slug}/order/{reference}?sent=1", status_code=303)
