"""
Who buys — the seller's view of their own buyers.

    GET /customers ──▶ everyone who has ordered · segments · a way to message

WHY A SELLER NEEDS THIS PAGE AND NOT ANOTHER CHART. The orders page answers
"what did I sell". This answers "who should I talk to next", which is the only
question that produces the next sale. A Nairobi seller's repeat buyers are their
whole business, and until now nothing in the workspace named them.

EVERY ROW ENDS IN A MESSAGE BUTTON. That is the point of the page: the seller is
already living in WhatsApp, so the useful action is not "view customer", it is
"open this conversation". There is no customer detail page, deliberately —
tapping a name to read a summary of what they bought helps nobody.

It reads; it never writes. Customers are derived from orders (see
``services/customers.py``), so there is nothing here to create or edit.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.dependencies import current_account
from app.models import Account
from app.services.customers import SEGMENTS, customer_summary, filter_segment, list_customers
from app.templating import templates

router = APIRouter(tags=["customers"])


@router.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
    q: str | None = Query(default=None, max_length=80),
    segment: str | None = Query(default=None, max_length=20),
) -> HTMLResponse:
    """
    Everyone who has bought from this shop.

    Args:
        request: For the template.
        account: Signed in, or the dependency redirects to /login.
        db: Session.
        q: Optional search over name, phone and location.
        segment: Optional segment key — see ``services.customers.SEGMENTS``.

    Returns:
        The list with its tiles and segments, or an empty state that explains
        where customers come from.

    Notes:
        THE TILES COUNT EVERYONE, THE LIST OBEYS THE FILTER. A seller narrowing
        to repeat buyers still needs to know they have 24 customers in total —
        summary numbers that move when you click a filter are the fastest way to
        make a dashboard untrustworthy.
    """
    seller = account.seller

    everyone = list_customers(db, seller, q) if seller is not None else []
    customers = filter_segment(everyone, segment)
    summary = (
        customer_summary(db, seller, everyone)
        if seller is not None
        else {
            "total": 0,
            "new_this_month": 0,
            "repeat": 0,
            "reachable": 0,
            "return_rate": 0,
            "reachable_percent": 0,
            "orders": 0,
            "high_value": 0,
            "payment_pending": 0,
        }
    )

    return templates.TemplateResponse(
        request,
        "app/customers.html",
        {
            "account": account,
            "seller": seller,
            "customers": customers,
            "summary": summary,
            "q": q or "",
            "segment": segment if segment in SEGMENTS else None,
            "segment_label": SEGMENTS[segment][0] if segment in SEGMENTS else None,
            # A wa.me share sheet carrying the storefront link. This is how a
            # seller actually gets customers, which makes it the right primary
            # action on the page that counts them.
            "share_url": (
                "https://wa.me/?text=" + quote(f"{get_settings().app_base_url}/shop/{seller.slug}")
                if seller is not None
                else None
            ),
            # A search that finds nothing is not the same as a shop with no
            # customers, and the two need different words on screen.
            "searching": bool(q and q.strip()),
            "nav": "customers",
        },
    )
