"""
The seller's home screen — a setup checklist, then a shop at a glance.

    GET /dashboard ──▶ what still stands between this seller and a first sale

THE EMPTY STATE IS THE PRODUCT'S FIRST IMPRESSION. Almost everyone who signs up
sees this page with nothing on it, and what it says decides whether they take
the actions that make the product work.

IT USED TO SAY "CONNECT YOUR TIKTOK". That belonged to the content-first
direction, which was parked: the catalogue no longer comes from a scrape, and a
seller who connects a social account still cannot sell anything. Connecting
TikTok is now optional and much later.

So it asks for the three things that actually stand between a new signup and a
first sale, in the order they have to happen:

    1. add a product        nothing to sell without one
    2. set up payments      checkout is blocked without one
    3. open the shop        the storefront 404s until it is open

Each step links straight to the screen that completes it. A checklist that
explains rather than acts is a page a seller reads once and leaves.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import current_account
from app.models import Account
from app.services.catalogue import catalogue_summary
from app.services.money import money_summary
from app.services.orders import get_payment_method
from app.templating import templates

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    What this seller has set up, and what is still missing.

    Args:
        request: For the template.
        account: Signed in, or the dependency redirects to /login.
        db: Session.

    Returns:
        The checklist while anything is outstanding; the shop at a glance once
        it is all done.
    """
    seller = account.seller

    catalogue = (
        catalogue_summary(db, seller)
        if seller is not None
        else {"published": 0, "draft": 0, "needs_price": 0}
    )
    payment_method = get_payment_method(db, seller) if seller is not None else None
    money = (
        money_summary(db, seller)
        if seller is not None
        else {"sales_this_month": 0, "paid_orders": 0, "average_order": 0, "awaiting_kes": 0}
    )

    # Each step is "done" only when the thing it unlocks is actually possible —
    # a draft product does not count, because a buyer cannot buy one.
    steps = {
        "has_product": catalogue["published"] + catalogue["draft"] > 0,
        "has_payment": payment_method is not None,
        "is_open": bool(seller and seller.is_published and catalogue["published"] > 0),
    }

    return templates.TemplateResponse(
        request,
        "app/dashboard.html",
        {
            "account": account,
            "seller": seller,
            "catalogue": catalogue,
            "payment_method": payment_method,
            "money": money,
            "steps": steps,
            "setup_complete": all(steps.values()),
            "nav": "dashboard",
        },
    )
