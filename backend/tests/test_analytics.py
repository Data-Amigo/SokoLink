"""
Tests for recording a scrape as posts and history.

Three things are protected here, and the last one is the reason the module
exists at all:

  **Every post is stored.** Commerce ingestion skips anything that is not a
  sellable item. Analytics must not — a face-to-camera video still has views,
  and filtering it out puts holes in the chart.

  **A re-sync updates, never duplicates.** And it must not overwrite the
  caption, which is the ground truth the AI drafted from.

  **History accumulates by DAY.** One row per post per day, so a seller
  pressing Refresh four times does not put steps into their own chart — while
  measurements on different days genuinely stack up. This is the part that
  cannot be rebuilt if it is wrong, because nobody will tell us later what a
  post had last Tuesday.

Nothing here calls a paid API. The profile is built by hand.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountMetricSnapshot, Post, PostMetricSnapshot
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor, TikTokVideo
from app.services.analytics import record_profile
from tests.factories import make_account, make_seller

DAY = date(2026, 8, 18)


def video(video_id: str = "7100000000000000001", **overrides: Any) -> TikTokVideo:
    """A validated video, shaped like the real payload."""
    data: dict[str, Any] = {
        "id": video_id,
        "text": "#mitumba bales available",
        "webVideoUrl": f"https://www.tiktok.com/@seller/video/{video_id}",
        "createTimeISO": "2026-08-08T09:51:55.000Z",
        "isPinned": False,
        "playCount": 365,
        "diggCount": 18,
        "commentCount": 2,
        "shareCount": 1,
        "collectCount": 4,
        "repostCount": 0,
        "hashtags": [{"name": "mitumba"}],
        "videoMeta": {"coverUrl": "https://cdn.tiktok.com/cover.jpg", "duration": 10},
        "mediaUrls": [],
    }
    data.update(overrides)
    return TikTokVideo.from_apify(data)


def profile(*videos: TikTokVideo, **author_overrides: Any) -> ScrapedProfile:
    """A scrape result carrying the given videos."""
    author: dict[str, Any] = {
        "name": "zumamitumbabales",
        "nickName": "ZUMA MITUMBA BALES",
        "signature": "contact 0105515839",
        "fans": 22000,
        "video": 1453,
        "heart": 85700,
    }
    author.update(author_overrides)
    return ScrapedProfile(
        author=TikTokAuthor.model_validate(author),
        videos=list(videos),
    )


def connected(db: Session) -> Any:
    """A verified account ready to record against."""
    return make_account(db, make_seller(db))


class TestRecordingPosts:
    def test_a_post_is_stored_with_everything_we_scraped(self, db: Session) -> None:
        account = connected(db)

        record_profile(db, account, profile(video()), captured_on=DAY)

        post = db.scalars(select(Post)).one()
        assert post.platform_post_id == "7100000000000000001"
        assert post.caption == "#mitumba bales available"
        assert post.hashtags == ["mitumba"]
        assert post.views == 365
        assert post.saves == 4

    def test_the_post_date_survives(self, db: Session) -> None:
        """Without posted_at there is no time axis, and no chart."""
        account = connected(db)

        record_profile(db, account, profile(video()), captured_on=DAY)

        post = db.scalars(select(Post)).one()
        assert post.posted_at is not None
        assert post.posted_at.date() == date(2026, 8, 8)

    def test_a_non_product_post_is_still_stored(self, db: Session) -> None:
        """
        THE reason this module is separate from ingestion.py.

        A talking-head video has views the creator is paying us to explain.
        Commerce ingestion skips it; analytics cannot.
        """
        account = connected(db)
        chatter = video("7100000000000000009", text="good morning everyone!", hashtags=[])

        record_profile(db, account, profile(chatter), captured_on=DAY)

        assert db.scalars(select(Post)).one().caption == "good morning everyone!"

    def test_no_ai_is_needed_to_record_a_profile(self, db: Session) -> None:
        """
        record_profile takes no drafter and no scraper — it cannot spend money.

        Asserted on the signature rather than by mocking, because the guarantee
        is structural: there is nothing to call.
        """
        import inspect

        params = set(inspect.signature(record_profile).parameters)
        assert params == {"db", "account", "profile", "captured_on"}

    def test_the_account_profile_is_refreshed(self, db: Session) -> None:
        account = connected(db)

        record_profile(db, account, profile(video()), captured_on=DAY)

        assert account.follower_count == 22000
        assert account.post_count == 1453
        assert account.last_synced_at is not None


class TestReSync:
    def test_a_second_sync_updates_rather_than_duplicating(self, db: Session) -> None:
        account = connected(db)
        record_profile(db, account, profile(video()), captured_on=DAY)

        result = record_profile(
            db, account, profile(video(playCount=9999)), captured_on=DAY + timedelta(days=1)
        )

        assert db.scalars(select(Post)).one().views == 9999
        assert result.created == []
        assert len(result.updated) == 1

    def test_a_re_sync_never_rewrites_the_caption(self, db: Session) -> None:
        """
        The caption is the ground truth the AI drafted from, and the text a
        prompt change is replayed against. Losing the original is permanent.
        """
        account = connected(db)
        record_profile(db, account, profile(video()), captured_on=DAY)

        record_profile(
            db,
            account,
            profile(video(text="edited by the creator later")),
            captured_on=DAY + timedelta(days=1),
        )

        assert db.scalars(select(Post)).one().caption == "#mitumba bales available"

    def test_pinning_does_move_on_a_re_sync(self, db: Session) -> None:
        """Creators pin and unpin constantly, and it decides the average."""
        account = connected(db)
        record_profile(db, account, profile(video()), captured_on=DAY)

        record_profile(
            db, account, profile(video(isPinned=True)), captured_on=DAY + timedelta(days=1)
        )

        assert db.scalars(select(Post)).one().is_pinned is True

    def test_first_seen_survives_a_re_sync(self, db: Session) -> None:
        """The only record of when we started watching."""
        account = connected(db)
        record_profile(db, account, profile(video()), captured_on=DAY)
        original = db.scalars(select(Post)).one().first_seen_at

        record_profile(db, account, profile(video()), captured_on=DAY + timedelta(days=1))

        assert db.scalars(select(Post)).one().first_seen_at == original


class TestHistory:
    def test_a_snapshot_is_written_for_the_post(self, db: Session) -> None:
        account = connected(db)

        record_profile(db, account, profile(video()), captured_on=DAY)

        snapshot = db.scalars(select(PostMetricSnapshot)).one()
        assert snapshot.captured_on == DAY
        assert snapshot.views == 365
        assert snapshot.saves == 4

    def test_a_snapshot_is_written_for_the_account(self, db: Session) -> None:
        account = connected(db)

        record_profile(db, account, profile(video()), captured_on=DAY)

        snapshot = db.scalars(select(AccountMetricSnapshot)).one()
        assert snapshot.follower_count == 22000
        assert snapshot.total_likes == 85700

    def test_two_syncs_on_the_same_day_keep_one_row(self, db: Session) -> None:
        """
        A seller pressing Refresh must not put steps into their own chart.

        Last write wins, which is also the freshest reading.
        """
        account = connected(db)
        record_profile(db, account, profile(video()), captured_on=DAY)
        record_profile(db, account, profile(video(playCount=400)), captured_on=DAY)

        snapshot = db.scalars(select(PostMetricSnapshot)).one()
        assert snapshot.views == 400

    def test_different_days_accumulate(self, db: Session) -> None:
        """This is the growth curve. If it does not stack, nothing else works."""
        account = connected(db)
        record_profile(db, account, profile(video(playCount=100)), captured_on=DAY)
        record_profile(
            db, account, profile(video(playCount=250)), captured_on=DAY + timedelta(days=1)
        )
        record_profile(
            db, account, profile(video(playCount=600)), captured_on=DAY + timedelta(days=2)
        )

        rows = db.scalars(select(PostMetricSnapshot).order_by(PostMetricSnapshot.captured_on)).all()
        assert [r.views for r in rows] == [100, 250, 600]

    def test_account_history_accumulates_by_day_too(self, db: Session) -> None:
        account = connected(db)
        record_profile(db, account, profile(video(), fans=22000), captured_on=DAY)
        record_profile(
            db, account, profile(video(), fans=22350), captured_on=DAY + timedelta(days=1)
        )

        rows = db.scalars(
            select(AccountMetricSnapshot).order_by(AccountMetricSnapshot.captured_on)
        ).all()
        assert [r.follower_count for r in rows] == [22000, 22350]


class TestDerivedNumbers:
    def test_engagement_sums_every_interaction(self, db: Session) -> None:
        account = connected(db)
        record_profile(db, account, profile(video()), captured_on=DAY)

        post = db.scalars(select(Post)).one()
        # likes 18 + comments 2 + shares 1 + saves 4 + reposts 0
        assert post.engagement == 25

    def test_engagement_rate_is_a_share_of_views(self, db: Session) -> None:
        account = connected(db)
        record_profile(db, account, profile(video(playCount=100)), captured_on=DAY)

        post = db.scalars(select(Post)).one()
        assert post.engagement_rate == 0.25

    def test_a_post_with_no_views_has_an_unknown_rate(self, db: Session) -> None:
        """
        None, not 0.0. A post nobody has seen has an UNKNOWN rate, and showing
        0% tells the creator something false about their own content.
        """
        account = connected(db)
        record_profile(db, account, profile(video(playCount=0)), captured_on=DAY)

        assert db.scalars(select(Post)).one().engagement_rate is None


class TestWarningsAreCarried:
    def test_scraper_warnings_reach_the_result(self, db: Session) -> None:
        """The scrape succeeded, so nothing else would ever mention them."""
        account = connected(db)
        scraped = profile(video())
        scraped.warnings.append("post 999: ValidationError: boom")

        result = record_profile(db, account, scraped, captured_on=DAY)

        assert result.warnings == ["post 999: ValidationError: boom"]
