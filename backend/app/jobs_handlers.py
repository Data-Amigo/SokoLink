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

from sqlalchemy.orm import Session

from app.models import Job, SocialAccount
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
