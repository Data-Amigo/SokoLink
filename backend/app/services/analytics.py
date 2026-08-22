"""
Recording what a scrape saw, so growth becomes answerable.

    ScrapedProfile ──┬──▶ upsert Post rows            (current state)
                     ├──▶ PostMetricSnapshot          (history, per day)
                     └──▶ AccountMetricSnapshot       (history, per day)

THIS MODULE CALLS NO AI. Not once. Analytics ingestion is scrape, store, done —
which is what makes the feature everyone uses daily the cheap one. Only
*commerce* ingestion pays for the price cascade, and only when a seller asks to
sell something.

EVERY POST IS STORED, product or not. ``services/ingestion.py`` skips anything
the model judges not a sellable item, which is right for a catalogue and wrong
here: a creator's face-to-camera video still has views, and views are what they
are paying us to explain. Filtering them out would put holes in the chart and
call it analytics.

WHY UPSERT RATHER THAN INSERT. A post is seen again on every sync. Its metrics
change; its caption does not. Re-inserting would duplicate, and deleting first
would lose ``first_seen_at`` — the only record of when we started watching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountMetricSnapshot, Post, PostMetricSnapshot, SocialAccount
from app.schemas.tiktok import ScrapedProfile, TikTokVideo


@dataclass(slots=True)
class RecordResult:
    """What one recording pass changed."""

    created: list[Post] = field(default_factory=list)
    updated: list[Post] = field(default_factory=list)

    #: Days of history written or refreshed. Counted separately from posts
    #: because a re-sync on the same day updates rows rather than adding them,
    #: and "nothing was added" would otherwise look like a failure.
    post_snapshots: int = 0
    account_snapshots: int = 0

    warnings: list[str] = field(default_factory=list)

    @property
    def total_posts(self) -> int:
        return len(self.created) + len(self.updated)


def record_profile(
    db: Session,
    account: SocialAccount,
    profile: ScrapedProfile,
    *,
    captured_on: date | None = None,
) -> RecordResult:
    """
    Store everything a profile scrape returned.

    Args:
        db: Session. **The caller commits** — this may be one part of a larger
            unit of work, and a service that commits on its own takes that
            choice away.
        account: The connected account the scrape belongs to.
        profile: What the scraper returned, already validated.
        captured_on: Which day this measurement belongs to. Injectable so tests
            can build a multi-day history without waiting three days; defaults
            to today in UTC.

    Returns:
        What was created, updated, and any post-level problems carried up from
        the scraper.
    """
    day = captured_on or datetime.now(UTC).date()
    result = RecordResult()

    # Posts the scraper could not parse. Carried through rather than dropped —
    # the scrape succeeded, so nothing else would ever mention them.
    result.warnings.extend(profile.warnings)

    _refresh_account(account, profile)
    _record_account_snapshot(db, account, profile, day=day, result=result)

    existing = _existing_posts(db, account)

    for video in profile.videos:
        post = existing.get(video.video_id)
        if post is None:
            post = _create_post(db, account, video)
            result.created.append(post)
        else:
            _update_post(post, video)
            result.updated.append(post)

        _record_post_snapshot(db, post, video, day=day, result=result)

    account.last_synced_at = datetime.now(UTC)
    db.flush()
    return result


def _existing_posts(db: Session, account: SocialAccount) -> dict[str, Post]:
    """Posts we already hold for this account, keyed by the platform's id."""
    rows = db.scalars(select(Post).where(Post.social_account_id == account.id)).all()
    return {p.platform_post_id: p for p in rows}


def _refresh_account(account: SocialAccount, profile: ScrapedProfile) -> None:
    """
    Update the account's own profile fields from the scrape.

    Follower and post counts move, and a display name or bio can change between
    syncs. Kept in step so the dashboard is not showing a name the creator
    abandoned last month.
    """
    author = profile.author
    account.display_name = author.display_name or account.display_name
    account.avatar_url = author.avatar_url or account.avatar_url
    account.bio = author.bio or account.bio
    account.follower_count = author.follower_count
    account.post_count = author.video_count


def _create_post(db: Session, account: SocialAccount, video: TikTokVideo) -> Post:
    """
    Store a post seen for the first time.

    Notes:
        ``cover_url`` holds the platform's URL here. It is signed and will
        expire; downloading our own copy belongs to the sync layer, which owns
        the scraper. Keeping the remote URL meanwhile means a missing cover is
        visibly broken rather than silently absent.
    """
    post = Post(
        social_account_id=account.id,
        platform=account.platform,
        platform_post_id=video.video_id,
        caption=video.caption,
        hashtags=video.hashtags,
        post_url=video.video_url,
        cover_url=video.cover_url,
        duration_seconds=video.duration_seconds,
        posted_at=video.posted_at,
        is_pinned=video.is_pinned,
        views=video.views,
        likes=video.likes,
        comments=video.comments,
        shares=video.shares,
        saves=video.saves,
        reposts=video.reposts,
        last_synced_at=datetime.now(UTC),
    )
    db.add(post)
    db.flush()
    return post


def _update_post(post: Post, video: TikTokVideo) -> None:
    """
    Refresh a post we already had.

    METRICS AND THE PINNED FLAG ONLY. The caption is deliberately not touched:
    it is the ground truth the AI drafted from, and the text a prompt change is
    replayed against, so silently rewriting it would invalidate a record we
    cannot rebuild. A creator editing a caption is rare; losing the original is
    permanent.

    ``is_pinned`` DOES move, because creators pin and unpin constantly and the
    flag decides whether a post counts toward their average.
    """
    post.views = video.views
    post.likes = video.likes
    post.comments = video.comments
    post.shares = video.shares
    post.saves = video.saves
    post.reposts = video.reposts
    post.is_pinned = video.is_pinned
    post.last_synced_at = datetime.now(UTC)


def _record_post_snapshot(
    db: Session, post: Post, video: TikTokVideo, *, day: date, result: RecordResult
) -> None:
    """
    Write today's measurement for one post, or refresh it.

    One row per post per day — see the module docstring in models/snapshot.py.
    A second sync on the same day overwrites rather than appending, so a seller
    pressing Refresh does not put steps into their own chart.
    """
    snapshot = db.scalar(
        select(PostMetricSnapshot).where(
            PostMetricSnapshot.post_id == post.id,
            PostMetricSnapshot.captured_on == day,
        )
    )

    if snapshot is None:
        snapshot = PostMetricSnapshot(post_id=post.id, captured_on=day)
        db.add(snapshot)

    snapshot.captured_at = datetime.now(UTC)
    snapshot.views = video.views
    snapshot.likes = video.likes
    snapshot.comments = video.comments
    snapshot.shares = video.shares
    snapshot.saves = video.saves
    snapshot.reposts = video.reposts

    result.post_snapshots += 1


def _record_account_snapshot(
    db: Session,
    account: SocialAccount,
    profile: ScrapedProfile,
    *,
    day: date,
    result: RecordResult,
) -> None:
    """Write today's account totals, or refresh them."""
    snapshot = db.scalar(
        select(AccountMetricSnapshot).where(
            AccountMetricSnapshot.social_account_id == account.id,
            AccountMetricSnapshot.captured_on == day,
        )
    )

    if snapshot is None:
        snapshot = AccountMetricSnapshot(social_account_id=account.id, captured_on=day)
        db.add(snapshot)

    snapshot.captured_at = datetime.now(UTC)
    snapshot.follower_count = profile.author.follower_count
    snapshot.post_count = profile.author.video_count
    snapshot.total_likes = profile.author.total_likes

    result.account_snapshots += 1
