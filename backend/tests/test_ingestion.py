"""
Tests for turning scraped posts into catalogue products.

Two things are protected here, and the second is easy to lose in a refactor:

  **Cost.** The once-per-day cooldown, skipping posts already drafted, and the
  video tier being opt-in. A regression in any of these arrives as a bill, not
  as a failing assertion â€” unless there is an assertion.

  **The re-sync rail.** A sync may only touch what it created. A seller who
  uploads stock by hand, syncs their feed, and watches it vanish does not come
  back.

Scraper and agent are both faked. No test calls a paid API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import IngestMethod, Platform, Product, ProductStatus, ScrapeStatus
from app.schemas.draft import ProductDraft
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor, TikTokVideo
from app.services.drafting import CascadeResult
from app.services.ingestion import IngestionError, sync_account
from app.services.scraper import ScraperError
from tests.factories import make_account, make_product, make_seller


def video(video_id: str = "7100000000000000001", **overrides: Any) -> TikTokVideo:
    data: dict[str, Any] = {
        "video_id": video_id,
        "caption": "#sandalsforwomen",
        "cover_url": "https://cdn.tiktok.com/cover.jpeg",
        "hashtags": ["sandalsforwomen"],
        "views": 365,
        "likes": 18,
    }
    data.update(overrides)
    return TikTokVideo.model_validate(data)


def draft(**overrides: Any) -> ProductDraft:
    data: dict[str, Any] = {
        "is_product": True,
        "name": "Ladies Flat Sandals",
        "description": "Assorted ladies sandals.",
        "price_kes": 3000,
        "unit_quantity": 30,
        "unit_label": "pairs",
        "price_evidence": "@3000 30pairs",
        "confidence": 0.9,
    }
    data.update(overrides)
    return ProductDraft.model_validate(data)


class FakeScraper:
    """Returns a canned profile, or raises."""

    def __init__(
        self,
        *,
        videos: list[TikTokVideo] | None = None,
        fail: str | None = None,
        media_fails: bool = False,
    ) -> None:
        self.videos = videos if videos is not None else [video()]
        self.fail = fail
        self.media_fails = media_fails
        self.calls = 0

    def fetch_profile(self, handle: str, limit: int = 30) -> ScrapedProfile:
        self.calls += 1
        if self.fail:
            raise ScraperError(self.fail)
        return ScrapedProfile(
            author=TikTokAuthor.model_validate(
                {
                    "name": handle,
                    "nickName": "ZUMA MITUMBA BALES",
                    "signature": "contact us 0105515839",
                    "fans": 22000,
                    "video": 1453,
                }
            ),
            videos=self.videos,
        )

    def fetch_video(self, url: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def download_media(self, url: str, expect: str) -> bytes:
        """
        Cover bytes, because ingestion stores its own copy of every cover.

        This used to raise NotImplementedError, which made every sync test fail
        for a reason that had nothing to do with what it was testing — and hid
        a real bug in ``store_cover``, which promised never to let an image
        failure cost a product but only caught ScraperError.
        """
        if self.media_fails:
            raise ScraperError("simulated cover download failure")
        return b"\xff\xd8\xff" + b"x" * 50_000  # JPEG magic bytes + filler


class FakeDrafter:
    """Returns a canned cascade result and records how it was called."""

    def __init__(self, result: ProductDraft | None = None) -> None:
        self._draft = result if result is not None else draft()
        self.calls = 0
        self.video_tier_allowed: list[bool] = []

    def draft_for_video(
        self,
        video: TikTokVideo,
        *,
        allow_video_tier: bool = True,
        platform: Platform = Platform.TIKTOK,
    ) -> CascadeResult:
        self.calls += 1
        self.video_tier_allowed.append(allow_video_tier)
        from app.models.enums import PriceSource

        return CascadeResult(
            draft=self._draft,
            price_source=PriceSource.VIDEO if self._draft.has_price else None,
            platform=platform,
        )


def connected(db: Session) -> Any:
    """A verified, connected TikTok account ready to sync."""
    return make_account(db, make_seller(db))


class TestSyncCreatesDrafts:
    def test_a_post_becomes_a_draft_product(self, db: Session) -> None:
        account = connected(db)
        result = sync_account(db, account, FakeScraper(), FakeDrafter())

        assert len(result.created) == 1
        product = result.created[0]
        assert product.title == "Ladies Flat Sandals"
        assert product.status == ProductStatus.DRAFT.value

    def test_bulk_pricing_survives_into_the_product(self, db: Session) -> None:
        """The finding that changed the schema must reach the database intact."""
        account = connected(db)
        result = sync_account(db, account, FakeScraper(), FakeDrafter())

        product = result.created[0]
        assert product.price_kes == 3000
        assert product.unit_quantity == 30
        assert product.price_display == "KES 3,000 for 30 pairs"
        assert product.price_evidence == "@3000 30pairs"

    def test_nothing_is_ever_published_by_a_sync(self, db: Session) -> None:
        """Publishing is a deliberate human act. A sync cannot perform one."""
        account = connected(db)
        result = sync_account(db, account, FakeScraper(), FakeDrafter())

        assert all(p.status == ProductStatus.DRAFT.value for p in result.created)

    def test_the_post_is_linked_back_to_its_account(self, db: Session) -> None:
        account = connected(db)
        result = sync_account(db, account, FakeScraper(), FakeDrafter())

        assert result.created[0].social_account_id == account.id

    def test_a_non_product_is_skipped_rather_than_stored(self, db: Session) -> None:
        """Storing junk leaves the seller deleting it, which is worse."""
        account = connected(db)
        result = sync_account(
            db,
            account,
            FakeScraper(),
            FakeDrafter(draft(is_product=False)),
        )

        assert result.created == []
        assert result.skipped_non_products == 1

    def test_the_profile_is_refreshed_from_the_platform(self, db: Session) -> None:
        account = connected(db)
        sync_account(db, account, FakeScraper(), FakeDrafter())

        assert account.follower_count == 22000
        assert account.post_count == 1453


class TestCostGuards:
    def test_a_second_sync_within_a_day_is_refused(self, db: Session) -> None:
        """Apify bills per post. A dashboard refresh must not re-scrape."""
        account = connected(db)
        scraper = FakeScraper()
        sync_account(db, account, scraper, FakeDrafter())

        with pytest.raises(IngestionError, match="synced recently"):
            sync_account(db, account, scraper, FakeDrafter())
        assert scraper.calls == 1, "the cooldown must be checked BEFORE the paid call"

    def test_force_overrides_the_cooldown(self, db: Session) -> None:
        """For a seller who has genuinely just posted."""
        account = connected(db)
        scraper = FakeScraper()
        sync_account(db, account, scraper, FakeDrafter())
        sync_account(db, account, scraper, FakeDrafter(), force=True)

        assert scraper.calls == 2

    def test_the_cooldown_expires(self, db: Session) -> None:
        account = connected(db)
        sync_account(db, account, FakeScraper(), FakeDrafter())
        account.last_synced_at = datetime.now(UTC) - timedelta(days=2)

        result = sync_account(db, account, FakeScraper(), FakeDrafter())
        assert result.job.status == ScrapeStatus.SUCCEEDED.value

    def test_an_already_drafted_post_is_not_re_drafted(self, db: Session) -> None:
        """That AI work is done and paid for. Re-running it pays twice."""
        account = connected(db)
        sync_account(db, account, FakeScraper(), FakeDrafter())
        account.last_synced_at = None

        drafter = FakeDrafter()
        result = sync_account(db, account, FakeScraper(), drafter)

        assert drafter.calls == 0, "no AI call for a post we already have"
        assert len(result.updated) == 1
        assert result.created == []

    def test_the_video_tier_is_off_by_default(self, db: Session) -> None:
        """A bulk sync that watched every clip would be ruinous."""
        account = connected(db)
        drafter = FakeDrafter()
        sync_account(db, account, FakeScraper(), drafter)

        assert drafter.video_tier_allowed == [False]

    def test_the_video_tier_can_be_requested(self, db: Session) -> None:
        account = connected(db)
        drafter = FakeDrafter()
        sync_account(db, account, FakeScraper(), drafter, allow_video_tier=True)

        assert drafter.video_tier_allowed == [True]


class TestReSyncRail:
    """A sync owns only what it created."""

    def test_a_re_sync_refreshes_metrics_only(self, db: Session) -> None:
        """
        Never the title or price: the seller may have corrected them, and a
        sync silently undoing that is the fastest way to lose their trust.
        """
        account = connected(db)
        sync_account(db, account, FakeScraper(), FakeDrafter())

        product = db.query(Product).one()
        product.title = "Corrected By Seller"
        product.price_kes = 2500
        account.last_synced_at = None

        sync_account(db, account, FakeScraper(videos=[video(views=9999)]), FakeDrafter())

        assert product.title == "Corrected By Seller"
        assert product.price_kes == 2500
        assert product.views == 9999

    def test_a_sync_never_touches_an_uploaded_product(self, db: Session) -> None:
        """The rail. A seller's hand-entered stock must survive a sync."""
        account = connected(db)
        seller = account.seller
        uploaded = make_product(
            db,
            seller,
            title="Hand Uploaded Item",
            platform=Platform.MANUAL.value,
            ingest_method=IngestMethod.UPLOAD.value,
            platform_post_id=None,
            price_kes=1200,
        )

        sync_account(db, account, FakeScraper(), FakeDrafter())

        assert uploaded.title == "Hand Uploaded Item"
        assert uploaded.price_kes == 1200
        assert db.query(Product).count() == 2

    def test_a_sync_never_touches_a_pasted_link_product(self, db: Session) -> None:
        account = connected(db)
        pasted = make_product(
            db,
            account.seller,
            title="From A Pasted Link",
            ingest_method=IngestMethod.SINGLE_LINK.value,
            platform_post_id="7100000000000000001",
            price_kes=800,
        )

        # Same post id as the scrape returns â€” but a different ingest method,
        # so the sync must not claim it.
        sync_account(db, account, FakeScraper(), FakeDrafter())

        assert pasted.title == "From A Pasted Link"
        assert pasted.price_kes == 800


class TestFailureHandling:
    def test_an_unverified_account_cannot_be_synced(self, db: Session) -> None:
        account = connected(db)
        account.is_active = False

        with pytest.raises(IngestionError, match="disconnected"):
            sync_account(db, account, FakeScraper(), FakeDrafter())

    def test_a_scrape_failure_is_recorded_before_it_raises(self, db: Session) -> None:
        """
        A failed sync the seller cannot see is indistinguishable from "you have
        no videos", which sends them away thinking we are broken.
        """
        account = connected(db)

        with pytest.raises(IngestionError, match="rate limited"):
            sync_account(db, account, FakeScraper(fail="rate limited"), FakeDrafter())

        from app.models import ScrapeJob

        job = db.query(ScrapeJob).one()
        assert job.status == ScrapeStatus.FAILED.value
        assert job.error is not None and "rate limited" in job.error

    def test_a_failed_sync_does_not_start_the_cooldown(self, db: Session) -> None:
        """Otherwise one Apify hiccup locks the seller out for a day."""
        account = connected(db)

        with pytest.raises(IngestionError):
            sync_account(db, account, FakeScraper(fail="boom"), FakeDrafter())

        assert account.last_synced_at is None

    def test_the_job_records_what_it_did(self, db: Session) -> None:
        account = connected(db)
        result = sync_account(db, account, FakeScraper(), FakeDrafter())

        assert result.job.status == ScrapeStatus.SUCCEEDED.value
        assert result.job.video_count == 1
        assert result.job.products_upserted == 1
        assert result.job.completed_at is not None
