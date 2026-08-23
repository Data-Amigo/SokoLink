"""
The seller's money tracker, and the job-status endpoint.

    GET /analytics              what sold, and what is waiting on the seller
    GET /jobs/{id}/status       tiny JSON, polled while a background job runs

THE PAGE CHANGED, THE URL DID NOT. This used to show views, followers and post
counts — the content-first direction, now parked. It shows money instead. The
route keeps its path so the nav and any saved link still resolve.

WHY A POLLING ENDPOINT RATHER THAN A WEBSOCKET. A sync takes seconds to
minutes and finishes once. Polling every two seconds is a handful of indexed
reads, survives a dropped connection with no reconnect logic, and needs no
server-side state. A websocket would be more elegant and buy nothing here.

THE STATUS ENDPOINT IS SCOPED TO THE SELLER. Job ids are sequential integers,
and a job's result names handles and post counts. Somebody else's job is a 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.db import get_db
from app.dependencies import current_account, current_seller
from app.models import Account, Seller
from app.services.jobs import get_job
from app.services.money import money_summary, recent_transactions
from app.templating import templates

router = APIRouter(tags=["analytics"])


@router.get("/analytics", response_class=HTMLResponse)
def money_page(
    request: Request,
    account: Account = Depends(current_account),
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    The seller's money tracker.

    REPLACED THE SOCIAL ANALYTICS PAGE. That showed views, followers and post
    counts, which belonged to the content-first direction that was parked. A
    seller running a shop opens the app asking one question: did I make any
    money, and is anyone waiting on me?

    The URL stays /analytics so existing links and the nav do not break; the
    page it serves is the money tracker.

    Args:
        request: For the template.
        account: Signed in, or the dependency redirects to /login.
        seller: Their shop.
        db: Session.

    Returns:
        Four totals and the recent orders behind them.
    """
    return templates.TemplateResponse(
        request,
        "app/money.html",
        {
            "account": account,
            "seller": seller,
            "summary": money_summary(db, seller),
            "transactions": recent_transactions(db, seller),
            "nav": "analytics",
        },
    )


@router.get("/jobs/{job_id}/status")
def job_status(
    job_id: int,
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> JSONResponse:
    """
    Where a job has got to. Polled by the page while a sync runs.

    Args:
        job_id: From the URL, and therefore attacker-controlled.
        seller: Scopes the lookup — somebody else's job is a 404.
        db: Session.

    Returns:
        Small JSON: status, whether it is finished, and the result or error.

    Raises:
        HTTPException: 404 when the job is missing or belongs to someone else.
            Deliberately indistinguishable.
    """
    found = get_job(db, job_id, seller_id=seller.id)
    if found is None:
        raise HTTPException(status_code=404, detail="Job not found")

    return JSONResponse(
        {
            "status": found.status,
            "finished": found.is_finished,
            "result": found.result,
            "error": found.error,
        }
    )
