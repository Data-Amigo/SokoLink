"""
Shop settings — currently, how the seller gets paid.

    GET  /settings/payment          the form
    POST /settings/payment          save it
    POST /settings/payment/disable-stk  withdraw the Daraja credentials

WHY THIS ROUTER EXISTS SEPARATELY FROM ``api/payments.py``. That one is the
buyer's side and contains a PUBLIC, unauthenticated callback endpoint. This one
is entirely behind the login wall and handles a seller's payment credentials.
Keeping them in one file would put a route that anyone may call next to routes
that must never be reachable, which is exactly the adjacency that produces an
accidentally-public handler during a refactor.

SECRETS ARE NEVER RENDERED BACK. The form shows whether credentials are saved,
never what they are. There is no code path from the database to a rendered
passkey, and a blank field on submit means "unchanged" rather than "delete" —
so a seller editing their till number is not forced to re-type a secret they
cannot see.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import current_account
from app.models import Account, PaymentMethod, PaymentMethodKind
from app.services.payment_methods import (
    PaymentSetupError,
    disable_stk,
    save_payment_method,
)
from app.templating import templates

router = APIRouter(tags=["settings"])


def _back(error: str | None = None, saved: bool = False) -> RedirectResponse:
    """Redirect after a POST so a refresh cannot resubmit credentials."""
    path = "/settings/payment"
    if error:
        path = f"{path}?error={error}"
    elif saved:
        path = f"{path}?saved=1"
    return RedirectResponse(url=path, status_code=303)


@router.get("/settings/payment", response_class=HTMLResponse)
def payment_settings(
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Where the seller says how they want to be paid.

    Until this is filled in, ``place_order`` refuses and the storefront cannot
    take a single order — so the page leads with that fact rather than burying
    it, and defaults to Pochi, which needs nothing from Safaricom.
    """
    seller = account.seller
    # Queried, not read off the relationship: it can be stale at None when
    # the row was written elsewhere in this session, and a page that says
    # "no payment method" to a seller who has one is actively misleading.
    method = (
        db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id))
        if seller is not None
        else None
    )

    return templates.TemplateResponse(
        request,
        "app/payment_settings.html",
        {
            "account": account,
            "seller": seller,
            "method": method,
            "kinds": list(PaymentMethodKind),
            "nav": "settings",
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/settings/payment")
def payment_settings_save(
    kind: str = Form(...),
    number: str = Form(...),
    account_name: str = Form(""),
    account_reference: str = Form(""),
    stk_shortcode: str = Form(""),
    consumer_key: str = Form(""),
    consumer_secret: str = Form(""),
    passkey: str = Form(""),
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """Save the seller's payment destination, encrypting anything secret."""
    seller = account.seller
    if seller is None:
        return _back("Your shop is not set up yet.")

    try:
        save_payment_method(
            db,
            seller,
            kind=kind,
            number=number,
            account_name=account_name,
            account_reference=account_reference,
            stk_shortcode=stk_shortcode,
            consumer_key=consumer_key,
            consumer_secret=consumer_secret,
            passkey=passkey,
        )
    except PaymentSetupError as exc:
        return _back(str(exc))

    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back(saved=True)


@router.post("/settings/payment/disable-stk")
def payment_settings_disable_stk(
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> Response:
    """
    Withdraw the Daraja credentials, keeping the shop and its orders.

    Custody should be reversible. A seller who changes their mind about handing
    us secrets goes back to confirming payments by hand — which is what most
    sellers do anyway.
    """
    seller = account.seller
    if seller is not None:
        disable_stk(db, seller)
    # Routes commit; get_db does not. See app/db.py.
    db.commit()

    return _back(saved=True)
