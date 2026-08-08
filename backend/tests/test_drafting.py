"""
Tests for the price cascade.

The behaviour worth protecting here is COST, not just correctness: the cascade
must stop the moment it has a confident price, and must never reach the video
tier unnecessarily. A regression that quietly escalates every item would pass a
naive correctness test and arrive as a bill.

Gemini and Apify are both faked. Tests never call a paid API.

Scenarios mirror the real spike results against @zumamitumbabales:
    tier 2 (cover) 0/4  ->  tier 3 (video) 3/3
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.draft import DraftAgentError
from app.models.enums import PriceSource
from app.schemas.draft import ProductDraft
from app.schemas.tiktok import TikTokVideo
from app.services.drafting import DraftingService
from app.services.scraper import ScraperError


def make_video(**overrides: Any) -> TikTokVideo:
    """A scraped video with a cover but no download URL, as a normal sync gives."""
    data: dict[str, Any] = {
        "video_id": "7671596309060570375",
        "caption": "#sandalsforwomen #zarasandals",
        "cover_url": "https://cdn.tiktok.com/cover.jpeg",
        "download_url": None,
        "hashtags": ["sandalsforwomen", "zarasandals"],
    }
    data.update(overrides)
    return TikTokVideo.model_validate(data)


def draft(**overrides: Any) -> ProductDraft:
    """A drafted product; unpriced and low-confidence unless overridden."""
    data: dict[str, Any] = {
        "is_product": True,
        "name": "Ladies Flat Sandals",
        "description": "Assorted ladies sandals.",
        "price_kes": None,
        "confidence": 0.4,
    }
    data.update(overrides)
    return ProductDraft.model_validate(data)


class FakeScraper:
    """Returns bytes, or raises whatever the test asked for."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.downloads: list[str] = []

    def download_media(self, url: str, expect: str = "image") -> bytes:
        self.downloads.append(expect)
        if self.fail_on == expect:
            raise ScraperError(f"simulated {expect} failure")
        return b"x" * 50_000

    def fetch_profile(self, handle: str, limit: int = 30) -> Any:  # pragma: no cover
        raise NotImplementedError

    def fetch_video(self, url: str) -> Any:  # pragma: no cover
        raise NotImplementedError


class FakeAgent:
    """Returns canned drafts and records which tiers were called."""

    def __init__(
        self,
        *,
        cover: ProductDraft | Exception | None = None,
        video: ProductDraft | Exception | None = None,
    ) -> None:
        self._cover = cover
        self._video = video
        self.calls: list[str] = []

    def draft_from_cover(self, _video: TikTokVideo, _image: bytes) -> ProductDraft:
        self.calls.append("cover")
        if isinstance(self._cover, Exception):
            raise self._cover
        assert self._cover is not None
        return self._cover

    def draft_from_video(self, _video: TikTokVideo, _clip: bytes) -> ProductDraft:
        self.calls.append("video")
        if isinstance(self._video, Exception):
            raise self._video
        assert self._video is not None
        return self._video


def service(scraper: FakeScraper, agent: FakeAgent) -> DraftingService:
    return DraftingService(agent=agent, scraper=scraper)  # type: ignore[arg-type]


class TestCostDiscipline:
    """The expensive tier must run only when the cheap one genuinely failed."""

    def test_a_confident_cover_price_stops_the_cascade(self) -> None:
        agent = FakeAgent(cover=draft(price_kes=1500, confidence=0.95))
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert agent.calls == ["cover"], "video tier must not run after a confident cover"
        assert result.price_source == PriceSource.COVER_IMAGE
        assert result.reached_video_tier is False

    def test_an_unpriced_cover_escalates_to_video(self) -> None:
        """The real @zumamitumbabales case: 0/4 covers, 3/3 videos."""
        agent = FakeAgent(
            cover=draft(price_kes=None, confidence=0.9),
            video=draft(price_kes=3000, confidence=0.9, unit_quantity=30, unit_label="pairs"),
        )
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert agent.calls == ["cover", "video"]
        assert result.price_source == PriceSource.VIDEO
        assert result.draft is not None
        assert result.draft.price_kes == 3000
        assert result.draft.unit_quantity == 30

    def test_a_low_confidence_cover_price_still_escalates(self) -> None:
        """A price we do not trust is not a reason to stop paying attention."""
        agent = FakeAgent(
            cover=draft(price_kes=999, confidence=0.3),
            video=draft(price_kes=3000, confidence=0.95),
        )
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert agent.calls == ["cover", "video"]
        assert result.draft is not None
        assert result.draft.price_kes == 3000

    def test_the_video_tier_can_be_forbidden_outright(self) -> None:
        """Bulk imports and over-quota sellers must never trigger paid clips."""
        agent = FakeAgent(cover=draft(price_kes=None))
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4"),
            allow_video_tier=False,
        )

        assert agent.calls == ["cover"]
        assert result.reached_video_tier is False
        assert any("not allowed" in w for w in result.warnings)

    def test_no_download_url_means_no_video_tier(self) -> None:
        """A normal sync has no video URL — that is expected, not an error."""
        agent = FakeAgent(cover=draft(price_kes=None))
        result = service(FakeScraper(), agent).draft_for_video(make_video())

        assert agent.calls == ["cover"]
        assert any("no download URL" in w for w in result.warnings)


class TestFailureHandling:
    """A failed tier must not abort the cascade, and must never be swallowed."""

    def test_a_failed_cover_still_lets_video_rescue_the_item(self) -> None:
        agent = FakeAgent(video=draft(price_kes=3000, confidence=0.9))
        result = service(FakeScraper(fail_on="image"), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert result.has_price is True
        assert any("cover tier failed" in w for w in result.warnings)

    def test_an_agent_error_on_cover_is_recorded_not_raised(self) -> None:
        agent = FakeAgent(
            cover=DraftAgentError("quota exhausted"),
            video=draft(price_kes=3000, confidence=0.9),
        )
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert result.has_price is True
        assert any("quota exhausted" in w for w in result.warnings)

    def test_both_tiers_failing_returns_a_result_not_an_exception(self) -> None:
        """
        The caller needs to record the failure against the scrape job, which it
        cannot do if the cascade explodes.
        """
        agent = FakeAgent(cover=DraftAgentError("cover boom"), video=DraftAgentError("video boom"))
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert result.succeeded is False
        assert len(result.warnings) == 2

    def test_a_cover_draft_survives_a_failed_video_tier(self) -> None:
        """Losing the words too, just because the price lookup failed, is worse."""
        agent = FakeAgent(
            cover=draft(price_kes=None, name="Ladies Flat Sandals"),
            video=DraftAgentError("video boom"),
        )
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert result.draft is not None
        assert result.draft.name == "Ladies Flat Sandals"
        assert result.has_price is False


class TestOutcomeRecording:
    """We must be able to ask later whether the expensive tier earns its cost."""

    def test_attempted_tiers_are_recorded(self) -> None:
        agent = FakeAgent(cover=draft(price_kes=None), video=draft(price_kes=3000))
        result = service(FakeScraper(), agent).draft_for_video(
            make_video(download_url="https://api.apify.com/x.mp4")
        )

        assert result.tiers_attempted == ["cover_image", "video"]

    def test_an_unpriced_result_is_a_legitimate_outcome(self) -> None:
        """The seller fills it in at review — that is the human gate working."""
        agent = FakeAgent(cover=draft(price_kes=None, confidence=0.8))
        result = service(FakeScraper(), agent).draft_for_video(make_video())

        assert result.succeeded is True
        assert result.has_price is False
        assert result.draft is not None
        assert result.draft.needs_review is True


class TestDraftSchema:
    """Guards on the draft itself, before it can ever reach the database."""

    def test_a_phone_number_priced_draft_is_implausible(self) -> None:
        assert draft(price_kes=712345678).is_plausible is False

    def test_a_normal_price_is_plausible(self) -> None:
        assert draft(price_kes=3000).is_plausible is True

    def test_confidence_outside_zero_to_one_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            ProductDraft.model_validate({"is_product": True, "name": "x", "confidence": 1.5})

    def test_a_priced_low_confidence_draft_still_needs_review(self) -> None:
        assert draft(price_kes=3000, confidence=0.5).needs_review is True

    def test_a_non_product_always_needs_review(self) -> None:
        assert draft(is_product=False, price_kes=3000, confidence=0.99).needs_review is True
