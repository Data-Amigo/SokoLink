"""
The seller's orders — and the one screen where money is confirmed.

    GET  /orders                     everything, newest first
    GET  /orders/{reference}         one order in full
    POST /orders/{reference}/confirm the seller vouches that money arrived
    POST /orders/{reference}/cancel  abandon it and return the stock

WHY THIS SCREEN IS THE POINT. On the manual path — Pochi la Biashara, and any
till without Daraja credentials — there is no callback and there never will be.
The only thing that turns "a buyer typed ten characters" into "money arrived" is
a human looking at their own M-Pesa messages and saying so. **This page is that
act.** Without it the manual path is a dead end, and the manual path is what most
of our sellers have.

EVERY ROUTE IS SCOPED TO THE SIGNED-IN SELLER'S SHOP. Order ids and references
are not secrets between sellers — a reference travels in a buyer's WhatsApp
message — so scoping is what stops one seller confirming, cancelling or reading
another's orders. It is applied in :func:`_load_own_order`, once, rather than in
each handler where one could forget.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload
from starlette.exceptions import HTTPException

from app.db import get_db
from app.dependencies import current_account
from app.models import Account, Order, OrderStatus
from app.services.orders import OrderError, cancel_order, confirm_payment, get_order
from app.templating import templates

router = APIRouter(tags=["orders"])


def _load_own_order(db: Session, account: Account, reference: str) -> Order:
    """
    An order, only if it belongs to this seller's shop.

    Raises:
        HTTPException: 404 when the reference is unknown OR belongs to another
            seller. Identical responses on purpose: distinguishing them would
            confirm to one seller that another's order reference is real.
    """
    seller = account.seller
    order = get_order(db, reference)
    if order is None or seller is None or order.seller_id != seller.id:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


def _back_to_orders(reference: str | None = None, error: str | None = None) -> RedirectResponse:
    """
    Redirect after a POST so a refresh cannot repeat it.

    303 forces the follow-up to be a GET. Confirming a payment twice is harmless
    today, but a seller refreshing after a cancel would otherwise be asked to
    re-cancel, and the pattern should not depend on which action it wraps.
    """
    path = f"/orders/{reference}" if reference else "/orders"
    if error:
        path = f"{path}?error={error}"
    return RedirectResponse(url=path, status_code=303)


@router.get("/orders", response_class=HTMLResponse)
def orders_page(
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Every order for this shop, newest first.

    Orders awaiting confirmation are counted separately and called out at the
    top. That number is the seller's actual to-do list: a buyer has said they
    paid and is waiting to be believed, and every hour it sits there is an hour
    somebody is wondering whether their money went nowhere.
    """
    seller = account.seller

    orders = (
        db.scalars(
            select(Order)
            .where(Order.seller_id == seller.id)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
        ).all()
        if seller is not None
        else []
    )

    awaiting = [o for o in orders if o.status == OrderStatus.AWAITING_CONFIRMATION.value]

    return templates.TemplateResponse(
        request,
        "app/orders.html",
        {
            "account": account,
            "seller": seller,
            "orders": orders,
            "awaiting": awaiting,
            "nav": "orders",
            "error": request.query_params.get("error"),
        },
    )


@router.get("/orders/{reference}", response_class=HTMLResponse)
def order_detail(
    reference: str,
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """One order in full, with the buyer's claimed code and the confirm action."""
    order = _load_own_order(db, account, reference)

    return templates.TemplateResponse(
        request,
        "app/order_detail.html",
        {
            "account": account,
            "seller": account.seller,
            "order": order,
            "nav": "orders",
            "error": request.query_params.get("error"),
        },
    )


@router.post("/orders/{reference}/confirm")
def order_confirm(
    reference: str,
    receipt: str = Form(""),
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """
    Settle an order: the seller has seen the money.

    ``receipt`` is optional — leaving it blank accepts the buyer's claimed code
    as-is, which is what a seller does when the code on their phone matches. A
    different value replaces it, for when the buyer mistyped.

    **This is the only way an order becomes paid on the manual path**, and it
    requires a human. Nothing automatic can do it, because nothing automatic can
    see the seller's M-Pesa inbox.
    """
    order = _load_own_order(db, account, reference)

    try:
        confirm_payment(db, order, receipt)
    except OrderError as exc:
        return _back_to_orders(reference, str(exc))

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back_to_orders(reference)


@router.post("/orders/{reference}/cancel")
def order_cancel(
    reference: str,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """
    Abandon an order and return its stock.

    A paid order cannot be cancelled here. We are not in the money path and
    cannot reverse anything — a refund is a conversation between the buyer and
    the seller, on the same M-Pesa line the payment used.
    """
    order = _load_own_order(db, account, reference)

    try:
        cancel_order(db, order)
    except OrderError as exc:
        return _back_to_orders(reference, str(exc))

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back_to_orders(reference)
