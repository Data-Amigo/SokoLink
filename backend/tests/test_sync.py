"""
Tests for the analytics sync — the path that spends money.

``test_analytics.py`` covers recording a scrape once it has happened. This
covers deciding whether to scrape at all, and what surrounds it:

  **The cooldown is checked twice, and both matter.** Once when the button is
  pressed, so the seller gets an instant readable answer; again in the worker
  before the paid call, because a job can wait in the queue while another sync
  completes. A test for each.

  **Requesting a sync spends nothing.** The scrape belongs to the worker. If
  the route ever scrapes, a 3-minute Apify run happens inside an HTTP request
  and the seller's retry buys it twice.

  **Covers become ours.** Platform cover URLs are signed and expire; a
  storefront of broken images is worse than no images.

The scraper is faked throughout. No test hits Apify.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobStatus, Post
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor, TikTokVideo
from app.services.scraper import ScraperError
from app.services.sync import (
    SYNC_COOLDOWN,
    SyncError,
    cooldown_remaining,
    recent_posts,
    request_sync,
    run_sync,
)
from tests.factories import make_account, make_seller


def video(video_id: str = "7100000000000000001", **overrides: Any) -> TikTokVideo:
    data: dict[str, Any] = {
        "id": video_id,
        "text": "#mitumba bales",
        "webVideoUrl": f"https://www.tiktok.com/@s/video/{video_id}",
        "createTimeISO": "2026-08-08T09:51:55.000Z",
        "playCount": 365,
        "diggCount": 18,
        "commentCount": 2,
        "shareCount": 1,
        "collectCount": 4,
        "hashtags": [{"name": "mitumba"}],
        "videoMeta": {"coverUrl": "https://cdn.tiktok.com/cover.jpg", "duration": 10},
        "mediaUrls": [],
    }
    data.update(overrides)
    return TikTokVideo.from_apify(data)


class FakeScraper:
    """Counts its calls, so a test can prove nothing was bought."""

    def __init__(self, *, videos: list[TikTokVideo] | None = None, fail: str | None = None) -> None:
        self.videos = videos if videos is not None else [video()]
        self.fail = fail
        self.calls = 0
        self.media_calls = 0

    def fetch_profile(self, handle: str, limit: int = 30) -> ScrapedProfile:
        self.calls += 1
        if self.fail:
            raise ScraperError(self.fail)
        return ScrapedProfile(
            author=TikTokAuthor.model_validate(
                {"name": handle, "nickName": "Seller", "fans": 22000, "video": 1453, "heart": 85700}
            ),
            videos=self.videos,
        )

    def fetch_video(self, url: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def download_media(self, url: str, expect: str) -> bytes:
        self.media_calls += 1
        return b"\xff\xd8\xff" + b"x" * 50_000  # JPEG magic + filler


def connected(db: Session) -> Any:
    return make_account(db, make_seller(db))


class TestRequestingASync:
    def test_requesting_queues_a_job_and_spends_nothing(self, db: Session) -> None:
        """The scrape belongs to the worker. A route must never do it."""
        account = connected(db)

        outcome = request_sync(db, account)

        assert outcome.job is not None
        assert outcome.job.kind == "sync_posts"
        assert outcome.job.payload["social_account_id"] == account.id
        assert outcome.job.status == JobStatus.QUEUED.value

    def test_a_second_press_collapses_onto_the_first(self, db: Session) -> None:
        account = connected(db)
        request_sync(db, account)

        outcome = request_sync(db, account)

        assert outcome.job is None
        assert outcome.already_queued is True
        assert db.scalar(select(Job).where(Job.kind == "sync_posts")) is not None

    def test_a_recent_sync_is_refused_with_a_number(self, db: Session) -> None:
        """ "Try again later" without a number reads as the product being broken."""
        account = connected(db)
        account.last_synced_at = datetime.now(UTC)

        with pytest.raises(SyncError, match="about 2[34] hours"):
            request_sync(db, account)

    def test_force_overrides_the_cooldown(self, db: Session) -> None:
        account = connected(db)
        account.last_synced_at = datetime.now(UTC)

        outcome = request_sync(db, account, force=True)

        assert outcome.job is not None
        assert outcome.job.payload["force"] is True

    def test_a_disconnected_account_cannot_be_synced(self, db: Session) -> None:
        account = connected(db)
        account.is_active = False

        with pytest.raises(SyncError, match="disconnected"):
            request_sync(db, account)


class TestTheCooldown:
    def test_a_never_synced_account_may_sync_now(self, db: Session) -> None:
        assert cooldown_remaining(connected(db)) is None

    def test_the_cooldown_expires(self, db: Session) -> None:
        account = connected(db)
        account.last_synced_at = datetime.now(UTC) - SYNC_COOLDOWN - timedelta(minutes=1)

        assert cooldown_remaining(account) is None

    def test_the_same_constant_governs_both_sync_paths(self) -> None:
        """
        Commerce and analytics scrape the same account and both stamp
        last_synced_at. Two constants would be two answers to one question.
        """
        from app.services.ingestion import SYNC_COOLDOWN as COMMERCE

        assert COMMERCE is SYNC_COOLDOWN


class TestRunningASync:
    def test_a_sync_records_posts_and_history(self, db: Session) -> None:
        account = connected(db)

        result = run_sync(db, account, FakeScraper())

        assert len(result.created) == 1
        assert result.post_snapshots == 1
        assert result.account_snapshots == 1
        assert db.scalars(select(Post)).one().views == 365

    def test_the_cooldown_is_re_checked_before_the_paid_call(self, db: Session) -> None:
        """
        A job can wait in the queue while another sync finishes. The guard that
        matters is the last one before money changes hands.
        """
        account = connected(db)
        account.last_synced_at = datetime.now(UTC)
        scraper = FakeScraper()

        with pytest.raises(SyncError, match="already synced"):
            run_sync(db, account, scraper)

        assert scraper.calls == 0, "the cooldown must be checked BEFORE the scrape"

    def test_a_scrape_failure_surfaces_its_reason(self, db: Session) -> None:
        """A private profile and a rate limit need different responses."""
        account = connected(db)

        with pytest.raises(SyncError, match="rate limited"):
            run_sync(db, account, FakeScraper(fail="rate limited"))

    def test_covers_are_replaced_with_our_own_copies(self, db: Session) -> None:
        """Platform cover URLs are signed and expire within days."""
        account = connected(db)

        run_sync(db, account, FakeScraper())

        cover = db.scalars(select(Post)).one().cover_url
        assert cover is not None
        assert not cover.startswith("http"), "the remote URL must not survive"
        assert cover.startswith("covers/")

    def test_a_re_sync_does_not_re_download_a_cover(self, db: Session) -> None:
        """The filename is keyed on the post id, so it is stable across syncs."""
        account = connected(db)
        scraper = FakeScraper()
        run_sync(db, account, scraper)
        first = scraper.media_calls

        account.last_synced_at = None
        run_sync(db, account, scraper)

        assert scraper.media_calls == first, "a stored cover must not be fetched again"

    def test_no_ai_is_involved(self, db: Session) -> None:
        """
        run_sync takes a scraper and no drafter. Analytics ingestion never pays
        for the price cascade — that is what keeps the daily feature cheap.
        """
        import inspect

        params = set(inspect.signature(run_sync).parameters)
        assert "drafter" not in params
        assert params == {"db", "account", "scraper", "limit", "force"}


class TestPostList:
    def test_posts_come_back_newest_first(self, db: Session) -> None:
        account = connected(db)
        run_sync(
            db,
            account,
            FakeScraper(
                videos=[
                    video("7100000000000000001", createTimeISO="2026-08-01T10:00:00.000Z"),
                    video("7100000000000000002", createTimeISO="2026-08-09T10:00:00.000Z"),
                ]
            ),
        )

        posts = recent_posts(db, [account.id])

        assert [p.platform_post_id for p in posts] == [
            "7100000000000000002",
            "7100000000000000001",
        ]

    def test_a_post_without_a_date_is_kept_and_sorted_last(self, db: Session) -> None:
        """
        Hiding it would make the totals disagree with the list — we still hold
        its metrics.
        """
        account = connected(db)
        run_sync(
            db,
            account,
            FakeScraper(
                videos=[
                    video("7100000000000000001", createTimeISO=None),
                    video("7100000000000000002", createTimeISO="2026-08-09T10:00:00.000Z"),
                ]
            ),
        )

        posts = recent_posts(db, [account.id])

        assert len(posts) == 2
        assert posts[-1].posted_at is None

    def test_no_accounts_means_no_query(self, db: Session) -> None:
        assert recent_posts(db, []) == []
