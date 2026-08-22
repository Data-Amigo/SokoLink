"""
Turning scraped posts into catalogue products.

    SocialAccount ──> scraper ──> cascade ──> Product rows (DRAFT)
          │                                        │
    require_syncable                        seller reviews
                                                   │
                                              PUBLISHED

WHERE THE MONEY GUARDS LIVE. Three of them, and each has cost a real bill
somewhere:

  1. **Once per day per account.** Apify bills per post scraped. Syncing on
     every dashboard load would make the unit economics fail exactly when the
     product succeeds.
  2. **Skip posts we already have.** A re-sync updates metrics but never
     re-runs the AI on a post already drafted — that work is done and paid for.
  3. **The video tier is opt-in per sync.** A bulk import that watched every
     clip would be ruinous; the seller triggers that deliberately, per item.

THE RE-SYNC RAIL. A sync may only touch products it created
(`ingest_method == profile_sync`). A seller who uploads stock by hand, syncs
their feed, and watches it vanish does not come back. Enforced here and tested.

Nothing in this module publishes anything. Everything arrives as DRAFT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    IngestMethod,
    Platform,
    Product,
    ProductStatus,
    ScrapeJob,
    ScrapeStatus,
    SocialAccount,
)
from app.schemas.tiktok import TikTokVideo
from app.services.drafting import Drafter
from app.services.media import store_cover
from app.services.scraper import DEFAULT_PROFILE_LIMIT, ScraperEngine, ScraperError
from app.services.sync import SYNC_COOLDOWN as _SYNC_COOLDOWN
from app.services.sync import cooldown_remaining as _cooldown_remaining
from app.services.verification import require_syncable

#: Re-exported from services/sync.py, which owns it.
#:
#: Both paths scrape the same account and both stamp ``last_synced_at``, so two
#: constants would be two answers to one question. Kept importable from here so
#: existing callers do not have to move.
SYNC_COOLDOWN = _SYNC_COOLDOWN


class IngestionError(Exception):
    """
    Ingestion could not proceed, with a message safe to show the seller.
    """


@dataclass(slots=True)
class SyncResult:
    """What one sync did, and what it cost."""

    job: ScrapeJob
    created: list[Product] = field(default_factory=list)
    updated: list[Product] = field(default_factory=list)
    skipped_non_products: int = 0

    #: Non-fatal problems. A post that fails to draft must not abort the sync —
    #: the other twenty are still worth having — but it is never swallowed.
    warnings: list[str] = field(default_factory=list)

    @property
    def total_touched(self) -> int:
        return len(self.created) + len(self.updated)


def sync_account(
    db: Session,
    account: SocialAccount,
    scraper: ScraperEngine,
    drafter: Drafter,
    *,
    limit: int = DEFAULT_PROFILE_LIMIT,
    allow_video_tier: bool = False,
    force: bool = False,
) -> SyncResult:
    """
    Pull an account's recent posts and draft any that are new.

    Args:
        db: Session. The caller commits.
        account: A connected — therefore verified — account.
        scraper: Ingestion engine.
        drafter: The price cascade.
        limit: Most recent N posts.
        allow_video_tier: Whether the expensive tier may run. **Default False**:
            a bulk sync that watched every clip would be ruinous. The seller
            escalates deliberately, per item, from the review queue.
        force: Skip the once-per-day cooldown. For a seller who has genuinely
            just posted and wants it now.

    Returns:
        What was created, updated, and what went wrong on the way.

    Raises:
        IngestionError: If the account cannot be synced, or the scrape failed
            outright. A failed job is recorded before raising, so the dashboard
            can explain itself.
    """
    try:
        require_syncable(account)
    except Exception as exc:
        raise IngestionError(str(exc)) from exc

    if not force:
        remaining = _cooldown_remaining(account)
        if remaining is not None:
            hours = max(1, int(remaining.total_seconds() // 3600))
            raise IngestionError(
                f"@{account.handle} was synced recently. Try again in about "
                f"{hours} hour{'s' if hours != 1 else ''}, or use Refresh to force it."
            )

    job = ScrapeJob(
        seller_id=account.seller_id,
        platform=account.platform,
        ingest_method=IngestMethod.PROFILE_SYNC.value,
        status=ScrapeStatus.RUNNING.value,
    )
    db.add(job)
    db.flush()

    try:
        profile = scraper.fetch_profile(account.handle, limit=limit)
    except ScraperError as exc:
        # Recorded before raising: a failed sync the seller cannot see is
        # indistinguishable from "you have no videos", which sends them away
        # thinking we are broken.
        job.status = ScrapeStatus.FAILED.value
        job.error = str(exc)
        job.completed_at = datetime.now(UTC)
        db.flush()
        raise IngestionError(str(exc)) from exc

    job.video_count = profile.video_count
    result = SyncResult(job=job)

    # Posts the scraper could not parse. Carried through rather than dropped:
    # the scrape succeeded, so nothing else would ever mention them.
    result.warnings.extend(profile.warnings)

    # Refresh the profile from the platform — the seller confirms rather than
    # retypes, and follower counts feed Soko Intel later.
    account.display_name = profile.author.display_name or account.display_name
    account.avatar_url = profile.author.avatar_url or account.avatar_url
    account.bio = profile.author.bio or account.bio
    account.follower_count = profile.author.follower_count
    account.post_count = profile.author.video_count

    existing = _existing_by_post_id(db, account)

    for video in profile.videos:
        try:
            if video.video_id in existing:
                _refresh_metrics(existing[video.video_id], video)
                result.updated.append(existing[video.video_id])
                continue

            product = _draft_new_product(
                db,
                account=account,
                video=video,
                scraper=scraper,
                drafter=drafter,
                allow_video_tier=allow_video_tier,
                result=result,
            )
            if product is not None:
                result.created.append(product)
        except Exception as exc:  # noqa: BLE001 — one bad post must not lose the rest
            # The TYPE matters as much as the message. Several exceptions carry
            # an empty str(), and a warning reading "post 123: " tells whoever
            # is debugging nothing at all.
            result.warnings.append(f"post {video.video_id}: {type(exc).__name__}: {exc}")

    account.last_synced_at = datetime.now(UTC)
    job.status = ScrapeStatus.SUCCEEDED.value
    job.products_upserted = result.total_touched
    job.completed_at = datetime.now(UTC)
    db.flush()

    return result


def _existing_by_post_id(db: Session, account: SocialAccount) -> dict[str, Product]:
    """
    Products this sync already owns, keyed by platform post id.

    Scoped to PROFILE_SYNC on purpose. A pasted link or an upload is not the
    sync's to touch, so they are invisible here — which is the re-sync rail
    expressed as a query rather than as a rule someone must remember.
    """
    rows = db.scalars(
        select(Product).where(
            Product.seller_id == account.seller_id,
            Product.platform == account.platform,
            Product.ingest_method == IngestMethod.PROFILE_SYNC.value,
        )
    ).all()
    return {p.platform_post_id: p for p in rows if p.platform_post_id}


def _refresh_metrics(product: Product, video: TikTokVideo) -> None:
    """
    Update engagement on a post we already drafted.

    Metrics only. The title, price, sizes and description are NEVER overwritten:
    the seller may have corrected them, and a sync silently undoing that
    correction is the fastest way to lose their trust. Re-running the AI would
    also pay again for work already done.
    """
    product.views = video.views
    product.likes = video.likes
    product.comments = video.comments
    product.shares = video.shares


def _draft_new_product(
    db: Session,
    *,
    account: SocialAccount,
    video: TikTokVideo,
    scraper: ScraperEngine,
    drafter: Drafter,
    allow_video_tier: bool,
    result: SyncResult,
) -> Product | None:
    """
    Run the cascade on one post and persist the draft.

    Returns:
        The new product, or None when the model judged it not a sellable item —
        a face, a shop interior, an announcement. Storing those would leave the
        seller deleting junk, which is worse than missing one product.
    """
    cascade = drafter.draft_for_video(
        video,
        allow_video_tier=allow_video_tier,
        platform=Platform(account.platform),
    )
    result.warnings.extend(cascade.warnings)

    # Store our own copy of the cover. Platform CDN URLs are signed and expire —
    # a shop whose images all break a week after import is worse than no shop.
    stored_cover: str | None = None
    if video.cover_url:
        stored_cover = store_cover(
            scraper,
            remote_url=video.cover_url,
            platform=account.platform,
            post_id=video.video_id,
        )
        if stored_cover is None:
            result.warnings.append(f"post {video.video_id}: cover could not be stored")

    draft = cascade.draft
    if draft is None:
        result.warnings.append(f"post {video.video_id}: no draft produced")
        return None

    if not draft.is_product:
        result.skipped_non_products += 1
        return None

    product = Product(
        seller_id=account.seller_id,
        social_account_id=account.id,
        platform=account.platform,
        ingest_method=IngestMethod.PROFILE_SYNC.value,
        platform_post_id=video.video_id,
        source_url=video.video_url,
        # Our stored path, not theirs. Falls back to the remote URL only when
        # storage failed, so a broken image is visibly a failure rather than
        # silently absent.
        cover_url=stored_cover or video.cover_url,
        raw_caption=video.caption,
        hashtags=video.hashtags,
        title=draft.name,
        description=draft.description or None,
        price_kes=draft.price_kes,
        unit_quantity=draft.unit_quantity,
        unit_label=draft.unit_label,
        price_evidence=draft.price_evidence,
        sizes=draft.sizes,
        parse_confidence=draft.confidence,
        price_source=cascade.price_source.value if cascade.price_source else None,
        views=video.views,
        likes=video.likes,
        comments=video.comments,
        shares=video.shares,
        # ALWAYS a draft. Publishing is a deliberate human act, and it still
        # requires a price — enforced by the database, not by this line.
        status=ProductStatus.DRAFT.value,
    )
    db.add(product)
    db.flush()
    return product
