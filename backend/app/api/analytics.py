"""
The analytics page, and the job-status endpoint that keeps it live.

    GET /analytics              the creator's posts
    GET /jobs/{id}/status       tiny JSON, polled while a sync runs

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
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.db import get_db
from app.dependencies import current_account, current_seller
from app.models import Account, Seller, SocialAccount
from app.services.jobs import get_job
from app.services.sync import cooldown_remaining, recent_posts
from app.templating import templates

router = APIRouter(tags=["analytics"])


@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    job: int | None = None,
    error: str | None = None,
    queued: int | None = None,
    account: Account = Depends(current_account),
    seller: Seller = Depends(current_seller),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """
    A creator's posts, newest first.

    Args:
        request: For the template.
        job: A job id to watch, set by the sync route's redirect.
        error: A message the sync route could not deliver any other way.
        queued: Set when a sync was already pending.
        account: Signed in, or the dependency redirects to /login.
        seller: Their shop.
        db: Session.

    Returns:
        The page, in whichever state is true: no account, nothing synced yet,
        or posts to show.
    """
    accounts = list(
        db.scalars(
            select(SocialAccount)
            .where(SocialAccount.seller_id == seller.id, SocialAccount.is_active.is_(True))
            .order_by(SocialAccount.follower_count.desc())
        ).all()
    )

    posts = recent_posts(db, [a.id for a in accounts])

    # Surfaced so the Sync button can explain itself rather than just refusing.
    waits = {a.id: cooldown_remaining(a) for a in accounts}

    return templates.TemplateResponse(
        request,
        "app/analytics.html",
        {
            "account": account,
            "seller": seller,
            "accounts": accounts,
            "posts": posts,
            "waits": waits,
            "watch_job": job,
            "error": error,
            "already_queued": bool(queued),
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
