"""
The creator's home screen.

    GET /dashboard ──> current_account ──> connected accounts ──> page
                              │
                        none connected ──> the empty state

THE EMPTY STATE IS THE PRODUCT'S FIRST IMPRESSION. Almost everyone who ever
signs up sees this page with nothing on it, and what it says decides whether
they take the one action that makes the product work. So it says one thing and
offers one button — not a tour, not a checklist, not a wall of features that do
not apply yet.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.dependencies import current_account
from app.models import Account, SocialAccount
from app.templating import templates

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    account: Account = Depends(current_account),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    What this creator has connected, and what to do next.

    Args:
        request: For the template.
        account: Signed in, or the dependency redirects to /login.
        db: Session.

    Returns:
        The dashboard, which is either a list of connected accounts or the
        empty state that asks for the first one.
    """
    seller = account.seller

    # Ordered by follower count so the account that matters most is first.
    # Inactive accounts are excluded rather than shown greyed out: a
    # disconnected account is not something to act on from here.
    accounts = (
        db.scalars(
            select(SocialAccount)
            .where(SocialAccount.seller_id == seller.id, SocialAccount.is_active.is_(True))
            .order_by(SocialAccount.follower_count.desc())
        ).all()
        if seller is not None
        else []
    )

    return templates.TemplateResponse(
        request,
        "app/dashboard.html",
        {
            "account": account,
            "seller": seller,
            "accounts": accounts,
            "nav": "dashboard",
        },
    )
