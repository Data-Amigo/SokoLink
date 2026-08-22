"""
Connecting a social account, and proving it belongs to you.

    GET  /accounts                      what is connected
    GET  /accounts/connect              enter a handle
    POST /accounts/connect              start_claim  ──> a code
    GET  /accounts/claim/{id}           show the code + instructions
    POST /accounts/claim/{id}/check     check_claim  ──> connected, or not yet
    POST /accounts/claim/{id}/cancel    throw the claim away

THIN. Every rule about what proves ownership lives in
``services/verification.py``, where it has thirty-five tests. This file reads a
form, calls the service, and renders the outcome.

TWO THINGS THIS FILE IS RESPONSIBLE FOR, and they are not in the service:

  **Ownership of the URL.** Claim ids are sequential integers. Every route here
  loads the claim through ``get_claim(db, id, seller_id)``, which returns None
  for somebody else's — rendered as a 404, so the endpoint cannot be used to
  count claims either.

  **Not spending money on a page load.** ``check_claim`` performs a paid scrape.
  It is reachable by POST only, never by GET — a GET would fire on every
  refresh, every back button and every link preview.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.db import get_db
from app.dependencies import current_account, current_seller
from app.models import Account, AccountClaim, Platform, Seller, SocialAccount
from app.models.account_claim import MAX_ATTEMPTS
from app.services.scraper import ScraperEngine, get_scraper
from app.services.sync import SyncError, request_sync
from app.services.verification import (
    VerificationError,
    check_claim,
    check_cooldown_remaining,
    get_claim,
    start_claim,
)
from app.templating import templates

router = APIRouter(tags=["accounts"])

#: Only TikTok has an ingestion engine. Instagram and Facebook exist in the
#: enum so the schema never has to change to admit them, but offering them here
#: would be a button that cannot work.
CONNECTABLE = Platform.TIKTOK


def _connected(db: Session, seller_id: int) -> list[SocialAccount]:
    """Every account this seller has proven, biggest first."""
    return list(
        db.scalars(
            select(SocialAccount)
            .where(SocialAccount.seller_id == seller_id, SocialAccount.is_active.is_(True))
            .order_by(SocialAccount.follower_count.desc())
        ).all()
    )


def _pending(db: Session, seller_id: int) -> AccountClaim | None:
    """A claim already in flight, if there is one."""
    return db.scalar(select(AccountClaim).where(AccountClaim.seller_id == seller_id))


def _claim_context(claim: AccountClaim) -> dict[str, object]:
    """
    Everything the code page needs to be honest about where the seller stands.

    Attempts left and the cooldown are surfaced rather than hidden: a limit the
    seller cannot see is indistinguishable from a broken button.
    """
    wait = check_cooldown_remaining(claim)
    return {
        "claim": claim,
        "attempts_left": max(0, MAX_ATTEMPTS - claim.attempts),
        "cooldown_seconds": int(wait.total_seconds()) + 1 if wait else 0,
    }


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    account: Account = Depends(current_account),
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """What this seller has connected, and what is still in flight."""
    return templates.TemplateResponse(
        request,
        "app/accounts.html",
        {
            "account": account,
            "seller": seller,
            "accounts": _connected(db, seller.id),
            "pending": _pending(db, seller.id),
            "nav": "accounts",
        },
    )


@router.get("/accounts/connect", response_class=HTMLResponse)
def connect_form(
    request: Request,
    account: Account = Depends(current_account),
) -> HTMLResponse:
    """Ask for the handle."""
    return templates.TemplateResponse(
        request,
        "app/connect.html",
        {"account": account, "platform": CONNECTABLE.value, "nav": "accounts"},
    )


@router.post("/accounts/connect", response_class=HTMLResponse)
def connect_submit(
    request: Request,
    handle: str = Form(...),
    account: Account = Depends(current_account),
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Mint a claim and send the seller to their code.

    Costs nothing — no scrape happens until they say they have added it.

    Returns:
        303 to the code page, or the form again at 400 with the reason. The
        reasons are worth showing verbatim: "already connected to your shop"
        and "verified by another shop" need very different responses.
    """
    try:
        claim = start_claim(db, seller.id, CONNECTABLE, handle)
    except VerificationError as exc:
        return templates.TemplateResponse(
            request,
            "app/connect.html",
            {
                "account": account,
                "platform": CONNECTABLE.value,
                "error": str(exc),
                "handle": handle,
                "nav": "accounts",
            },
            status_code=400,
        )

    db.commit()
    return RedirectResponse(f"/accounts/claim/{claim.id}", status_code=303)  # type: ignore[return-value]


@router.get("/accounts/claim/{claim_id}", response_class=HTMLResponse)
def claim_page(
    claim_id: int,
    request: Request,
    account: Account = Depends(current_account),
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    Show the code and how to use it.

    Raises:
        HTTPException: 404 if the claim is missing OR belongs to someone else.
            The two are deliberately indistinguishable.
    """
    claim = get_claim(db, claim_id, seller.id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    return templates.TemplateResponse(
        request,
        "app/claim.html",
        {"account": account, "nav": "accounts", **_claim_context(claim)},
    )


@router.post("/accounts/claim/{claim_id}/check", response_class=HTMLResponse)
def claim_check(
    claim_id: int,
    request: Request,
    account: Account = Depends(current_account),
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
    scraper: ScraperEngine = Depends(get_scraper),
) -> HTMLResponse:
    """
    Re-read the bio and connect the account if the code is there.

    **This is the only route in the app that spends money on a button press.**
    POST only, so a refresh cannot repeat it, and the service enforces both the
    attempt cap and the twenty-second cooldown before the scrape happens.

    Returns:
        303 to the dashboard once connected. Otherwise the same page, with
        either "not there yet" (an ordinary outcome — they probably have not
        saved) or the error the service raised.
    """
    claim = get_claim(db, claim_id, seller.id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    try:
        connected = check_claim(db, claim, scraper)
    except VerificationError as exc:
        # The attempt counter and cooldown stamp are still worth keeping even
        # when the scrape failed — they are what stops a retry loop.
        db.commit()
        return templates.TemplateResponse(
            request,
            "app/claim.html",
            {"account": account, "nav": "accounts", "error": str(exc), **_claim_context(claim)},
            status_code=400,
        )

    db.commit()

    if connected is None:
        return templates.TemplateResponse(
            request,
            "app/claim.html",
            {
                "account": account,
                "nav": "accounts",
                # Not an error. The overwhelmingly likely cause is that they
                # have not pressed Save in the TikTok app yet, and calling that
                # a failure makes a working product feel broken.
                "not_found": True,
                **_claim_context(claim),
            },
        )

    return RedirectResponse("/dashboard?connected=1", status_code=303)  # type: ignore[return-value]


@router.post("/accounts/claim/{claim_id}/cancel")
def claim_cancel(
    claim_id: int,
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """
    Throw a claim away so the seller can start again with a new code.

    The escape hatch for a mistyped handle or an exhausted code. Deleting is
    right rather than marking cancelled: a claim proves nothing, so there is
    nothing worth keeping.
    """
    claim = get_claim(db, claim_id, seller.id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    db.delete(claim)
    db.commit()
    return RedirectResponse("/accounts/connect", status_code=303)


@router.post("/accounts/{account_id}/sync")
def sync_account_route(
    account_id: int,
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """
    Ask for a sync. Queues the work and returns immediately.

    **Nothing is scraped in this request.** A profile scrape takes thirty
    seconds to three minutes; doing it here would time out and the seller would
    press again, buying the same scrape twice. The worker does it.

    Raises:
        HTTPException: 404 if the account is not this seller's. Scoped for the
            same reason claims are — ids are guessable, and a sync spends money.

    Returns:
        A 303 to the analytics page, carrying the job id so the page can poll
        for it. A duplicate press lands on the same page watching the job the
        first press queued.
    """
    account = db.scalar(
        select(SocialAccount).where(
            SocialAccount.id == account_id, SocialAccount.seller_id == seller.id
        )
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    try:
        outcome = request_sync(db, account)
    except SyncError as exc:
        db.rollback()
        return RedirectResponse(f"/analytics?error={quote(str(exc))}", status_code=303)

    db.commit()

    if outcome.job is not None:
        return RedirectResponse(f"/analytics?job={outcome.job.id}", status_code=303)

    # Already queued by an earlier press. Send them to watch it rather than
    # telling them off for pressing twice.
    return RedirectResponse("/analytics?queued=1", status_code=303)
