"""
Job handlers — what the worker actually runs.

    HANDLERS["sync_posts"] ──▶ sync_posts_handler(db, job)

WHY THEY LIVE IN ONE MODULE. The worker imports exactly this file to populate
its registry. One import, one place to look for "what can this system be asked
to do", and no risk of a handler existing but never being registered because
nothing imported it.

THREE RULES FOR A HANDLER, all of them learned the expensive way elsewhere:

  1. **Never commit.** The worker owns the transaction so a handler that raises
     halfway leaves nothing half-written.
  2. **Never swallow an exception.** Raising is how a job is marked failed;
     catching and returning quietly marks it succeeded and loses the problem.
  3. **Read from the payload's ids, not from objects.** A job can sit in the
     queue for minutes, and the world moves on while it waits.

Handlers stay thin: they resolve ids, call a service, and return a small dict
for the UI. The logic belongs in ``services/``, where routes can reach it too.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, Seller, SocialAccount
from app.services import outbound, whatsapp_cloud
from app.services.bot import summarise_intake
from app.services.intake import PARSE_FORWARD, IntakeError, ingest_forwarded_post
from app.services.scraper import get_scraper
from app.services.sync import SYNC_POSTS, run_sync
from app.worker import register


@register(SYNC_POSTS)
def sync_posts_handler(db: Session, job: Job) -> dict[str, Any] | None:
    """
    Pull an account's posts and record them, with their metric history.

    **This is the handler that spends money.** It calls Apify once per run,
    billed per post. The cooldown inside ``run_sync`` is checked again here,
    nearest the spend, because a job can wait in the queue while another sync
    finishes.

    Args:
        db: Session owned by the worker. **Do not commit.**
        job: Carries ``{"social_account_id": int, "force": bool}``.

    Returns:
        A summary small enough to render: what was found, and what went wrong
        on the way.

    Raises:
        ValueError: If the payload is malformed.
        LookupError: If the account has gone since the job was queued — a real
            possibility, because a seller can disconnect while work is pending.
        SyncError: If the account cannot be synced or the scrape failed. All of
            these mark the job failed, which is the honest outcome.
    """
    account_id = job.payload.get("social_account_id")
    if account_id is None:
        raise ValueError("sync_posts requires social_account_id in the payload")

    account = db.get(SocialAccount, account_id)
    if account is None:
        raise LookupError(f"Social account {account_id} no longer exists")

    result = run_sync(
        db,
        account,
        get_scraper(),
        force=bool(job.payload.get("force")),
    )

    return {
        "handle": account.handle,
        "posts_new": len(result.created),
        "posts_updated": len(result.updated),
        "snapshots": result.post_snapshots,
        # Truncated: the result renders in a browser, and a hundred warnings
        # from a bad payload would be a wall of text nobody reads.
        "warnings": result.warnings[:5],
    }


@register(PARSE_FORWARD)
def parse_forward_handler(db: Session, job: Job) -> dict[str, Any] | None:
    """
    Read one forwarded photo into a draft; if it is the last of the burst, send
    the seller one summary.

    WHY THE PARSE IS HERE AND NOT IN THE WEBHOOK. It is the one paid call in the
    forwarding path and it is slow, and Meta redelivers any webhook that does
    not answer at once. Run in the request, parsing a seller's whole catalogue
    was serial vision calls that timed out and came back out of order — the
    hour-long trickle this change exists to remove.

    WHY THE LAST JOB SENDS THE SUMMARY. A single worker runs jobs one at a time,
    so "no other parse job for this seller is still queued or running" means
    this is the last of the burst — no clock, no debounce window, no race to
    guard. It reads the whole batch of drafts and sends one message.

    Args:
        db: Session owned by the worker. **Do not commit.**
        job: Carries ``{"seller_id": int, "media_id": str, "caption": str}``.

    Returns:
        A small dict for the UI: what this photo became, and whether this job
        was the one that summarised the burst.

    Raises:
        ValueError: If the payload is malformed.
        LookupError: If the seller has gone since the job was queued.
    """
    seller_id = job.payload.get("seller_id")
    media_id = job.payload.get("media_id")
    caption = job.payload.get("caption") or ""
    if seller_id is None or not media_id:
        raise ValueError("parse_forward requires seller_id and media_id in the payload")

    seller = db.get(Seller, seller_id)
    if seller is None:
        raise LookupError(f"Seller {seller_id} no longer exists")

    note: dict[str, Any] = {"media_id": media_id}
    try:
        result = ingest_forwarded_post(
            db,
            seller,
            media_id=media_id,
            fetch=lambda: whatsapp_cloud.download_media(media_id),
            caption=caption,
        )
        note["product_id"] = result.product.id
        note["needs_price"] = result.needs_price
    except IntakeError as exc:
        # A PHOTO WE CANNOT READ IS A BUSINESS OUTCOME, NOT A JOB FAILURE. The
        # error is already cached against the media id, so a redelivery is not
        # re-billed, and the seller hears about it in the summary rather than
        # through a failed job nobody looks at. This is the one place a handler
        # here does not re-raise, and it is deliberate: raising would retry a
        # paid call that fails again the same way.
        note["skipped"] = str(exc)

    if not _forward_batch_still_running(db, seller_id, job.id):
        to = seller.whatsapp_number
        replies = summarise_intake(db, seller)
        if to:
            for reply in replies:
                outbound.send_reply(to, reply)
        note["summarised"] = True

    return note


def _forward_batch_still_running(db: Session, seller_id: int, current_job_id: int) -> bool:
    """
    Whether any OTHER parse_forward job for this seller is still to run.

    The current job is RUNNING, so it is excluded by id. Any sibling is QUEUED
    (a single worker has not reached it yet); when there are none, this job is
    the last of the burst and owns the summary.
    """
    remaining = db.scalar(
        select(func.count(Job.id)).where(
            Job.kind == PARSE_FORWARD,
            Job.seller_id == seller_id,
            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value]),
            Job.id != current_job_id,
        )
    )
    return bool(remaining)
