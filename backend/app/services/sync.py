"""
Pulling a connected account's posts — the analytics sync.

    request_sync()  ──▶ Job(queued)        cheap, instant, no scrape
                             │
                        worker claims
                             │
    run_sync()      ──▶ scrape ──▶ store covers ──▶ record_profile()
                                                          │
                                              Posts + daily snapshots

TWO ENTRY POINTS ON PURPOSE. ``request_sync`` is what a button calls: it
refuses politely if the account was synced recently, and returns immediately.
``run_sync`` is what the worker calls, and it is the only one that spends money.

WHERE THE MONEY GUARDS ARE, and why each is where it is:

  **The cooldown is checked twice.** Once in ``request_sync`` so the seller gets
  an instant, readable answer, and again in ``run_sync`` before the paid call —
  because a job can sit in the queue while another sync completes, and the
  check that matters is the one nearest the spend.

  **The dedupe key is on the job.** Two presses of Sync collapse into one
  queued job; see models/job.py.

  **No AI runs here at all.** Scrape, store, done. That is what makes the
  feature everyone uses daily the cheap one — only *commerce* ingestion pays
  for the price cascade.

THE COOLDOWN CONSTANT LIVES HERE and ``services/ingestion.py`` imports it.
Both paths scrape the same account and both stamp ``last_synced_at``, so two
constants would be two answers to one question.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Job, Post, SocialAccount
from app.services.analytics import RecordResult, record_profile
from app.services.jobs import enqueue
from app.services.media import store_cover
from app.services.scraper import DEFAULT_PROFILE_LIMIT, ScraperEngine, ScraperError

#: How long a sync's results stay fresh.
#:
#: Apify bills per post scraped. A seller refreshing the dashboard must not
#: re-scrape, or the unit economics fail exactly when the product succeeds.
SYNC_COOLDOWN = timedelta(hours=24)

#: The job kind. Named once so the enqueuer and the handler cannot disagree.
SYNC_POSTS = "sync_posts"


class SyncError(Exception):
    """Sync could not proceed, with a message safe to show the seller."""


@dataclass(slots=True)
class SyncOutcome:
    """What a sync request did."""

    job: Job | None

    #: True when an identical sync was already pending. Not a failure — the
    #: work the seller asked for is already on its way.
    already_queued: bool = False


def cooldown_remaining(account: SocialAccount) -> timedelta | None:
    """
    How long until this account may be scraped again.

    Args:
        account: The connected account.

    Returns:
        The wait remaining, or None if it may be synced now.
    """
    last = account.last_synced_at
    if last is None:
        return None

    # Rows can come back naive depending on driver history; treat as UTC rather
    # than crashing on the comparison.
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)

    elapsed = datetime.now(UTC) - last
    return None if elapsed >= SYNC_COOLDOWN else SYNC_COOLDOWN - elapsed


def dedupe_key_for(account: SocialAccount) -> str:
    """The key that collapses two Sync presses into one job."""
    return f"{SYNC_POSTS}:{account.id}"


def request_sync(db: Session, account: SocialAccount, *, force: bool = False) -> SyncOutcome:
    """
    Ask for a sync. Queues work; spends nothing.

    Args:
        db: Session. The caller commits.
        account: The account to sync.
        force: Skip the cooldown, for a seller who has genuinely just posted.

    Returns:
        The queued job, or an outcome saying one was already pending.

    Raises:
        SyncError: If the account is disconnected, or was synced too recently.
            The message names the wait, because "try again later" without a
            number reads as the product being broken.
    """
    if not account.can_sync:
        raise SyncError(f"@{account.handle} is disconnected. Reconnect it to sync.")

    if not force:
        wait = cooldown_remaining(account)
        if wait is not None:
            hours = max(1, int(wait.total_seconds() // 3600))
            raise SyncError(
                f"@{account.handle} was synced recently. Try again in about "
                f"{hours} hour{'s' if hours != 1 else ''}."
            )

    job = enqueue(
        db,
        SYNC_POSTS,
        payload={"social_account_id": account.id, "force": force},
        seller_id=account.seller_id,
        dedupe_key=dedupe_key_for(account),
    )

    return SyncOutcome(job=job, already_queued=job is None)


def run_sync(
    db: Session,
    account: SocialAccount,
    scraper: ScraperEngine,
    *,
    limit: int = DEFAULT_PROFILE_LIMIT,
    force: bool = False,
) -> RecordResult:
    """
    Do the work: scrape, store covers, record posts and history.

    Args:
        db: Session. **The caller commits** — the worker owns the transaction.
        account: The account to sync.
        scraper: Ingestion engine. This is the thing that costs money.
        limit: Most recent N posts.
        force: Skip the cooldown.

    Returns:
        What was recorded.

    Raises:
        SyncError: If the account cannot be synced, or the scrape failed. The
            worker turns this into a failed job with a readable message.
    """
    if not account.can_sync:
        raise SyncError(f"@{account.handle} is disconnected.")

    # Re-checked here, nearest the spend. A job can wait in the queue while
    # another sync finishes, and the guard that matters is the last one before
    # money changes hands.
    if not force:
        wait = cooldown_remaining(account)
        if wait is not None:
            raise SyncError(f"@{account.handle} was already synced within the last day.")

    try:
        profile = scraper.fetch_profile(account.handle, limit=limit)
    except ScraperError as exc:
        raise SyncError(str(exc)) from exc

    result = record_profile(db, account, profile)

    _store_covers(db, scraper, result, account)

    return result


def _store_covers(
    db: Session,
    scraper: ScraperEngine,
    result: RecordResult,
    account: SocialAccount,
) -> None:
    """
    Replace expiring platform cover URLs with our own stored copies.

    Notes:
        Done HERE rather than in ``services/analytics.py`` because downloading
        needs the scraper, and analytics deliberately takes neither a scraper
        nor a drafter — that is what makes it provably unable to spend money.

        Only posts still holding a remote URL are fetched. ``store_cover`` keys
        the filename on the post id, so it is stable across syncs and returns
        early when the file already exists — a re-sync costs no bandwidth.

        A failed download costs a placeholder image, never the post: the URL is
        left as the remote one so a broken image is visibly broken rather than
        silently absent.
    """
    for post in result.created + result.updated:
        remote = post.cover_url
        if not remote or not remote.startswith(("http://", "https://")):
            continue

        stored = store_cover(
            scraper,
            remote_url=remote,
            platform=account.platform,
            post_id=post.platform_post_id,
        )
        if stored is not None:
            post.cover_url = stored
        else:
            result.warnings.append(f"post {post.platform_post_id}: cover could not be stored")

    db.flush()


def recent_posts(db: Session, account_ids: list[int], limit: int = 50) -> list[Post]:
    """
    A seller's posts, newest first.

    Args:
        db: Session.
        account_ids: Which accounts to include.
        limit: How many to return.

    Returns:
        Posts ordered by when they were published, most recent first. Posts
        with no ``posted_at`` sort last rather than being dropped — we still
        hold their metrics, and hiding a post because its timestamp is missing
        would make the totals disagree with the list.
    """
    from sqlalchemy import select

    if not account_ids:
        return []

    return list(
        db.scalars(
            select(Post)
            .where(Post.social_account_id.in_(account_ids))
            .order_by(Post.posted_at.desc().nullslast(), Post.id.desc())
            .limit(limit)
        ).all()
    )
