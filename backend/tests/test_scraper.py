"""
Tests for TikTok ingestion.

Fixtures are built from the REAL Apify payload captured by spike 01 against
@zumamitumbabales, not from invented data. A test that passes against a payload
we imagined proves nothing about the one the provider actually sends.

Apify is always mocked. Tests never call a paid API — that is a hard rule, and
also the only way this suite can run on a fork or in CI.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.schemas.tiktok import TikTokAuthor, TikTokVideo
from app.services.scraper import ApifyEngine, ScraperError

# ── Fixtures shaped exactly like the real payload ────────────────────────────

REAL_AUTHOR: dict[str, Any] = {
    "name": "zumamitumbabales",
    "nickName": "ZUMA MITUMBA BALES",
    "avatar": "https://p16-sign.tiktokcdn-us.com/avatar.jpeg?x-expires=123",
    "signature": "zuma bales and LADIESWEAr contact us 0105515839/0754234636",
    "fans": 22000,
    "video": 1453,
    "heart": 85700,
    "verified": False,
    "privateAccount": False,
    "commerceUserInfo": {"commerceUser": False},
}

REAL_ITEM: dict[str, Any] = {
    "id": "7671596309060570375",
    "text": "#kenyansingulf #sidehustles #sandalsforwomen #zarasandals",
    "webVideoUrl": "https://www.tiktok.com/@zumamitumbabales/video/7671596309060570375",
    "playCount": 365,
    "diggCount": 18,
    "commentCount": 0,
    "shareCount": 1,
    "collectCount": 1,
    "isAd": False,
    "isPinned": False,
    "hashtags": [
        {"name": "kenyansingulf"},
        {"name": "sidehustles"},
        {"name": "SandalsForWomen"},
    ],
    "videoMeta": {
        "coverUrl": "https://p19-common-sign.tiktokcdn-us.com/cover.jpeg?x-expires=456",
        "originalCoverUrl": "https://p19-common-sign.tiktokcdn-us.com/orig.jpeg",
        "duration": 10,
        "height": 1024,
        "width": 576,
        "format": "mp4",
        "subtitleLinks": None,
        "transcriptionLink": None,
    },
    "authorMeta": REAL_AUTHOR,
    "mediaUrls": [],
}


def item(**overrides: Any) -> dict[str, Any]:
    """A real-shaped dataset item, with fields overridable per test."""
    return {**REAL_ITEM, **overrides}


class FakeResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        json_data: Any = None,
        content: bytes = b"",
        content_type: str = "application/json",
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self.content = content
        self.headers = {"content-type": content_type}
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture
def engine() -> ApifyEngine:
    """An engine with fake credentials — nothing here reaches the network."""
    return ApifyEngine(token="test-token", actor_id="test~actor")


# ── Schema: the validation border ────────────────────────────────────────────


class TestTikTokVideoParsing:
    def test_parses_a_real_apify_item(self) -> None:
        video = TikTokVideo.from_apify(item())

        assert video.video_id == "7671596309060570375"
        assert video.views == 365
        assert video.likes == 18
        assert video.duration_seconds == 10

    def test_flattens_the_nested_cover_url(self) -> None:
        """coverUrl lives inside videoMeta; aliases cannot reach into nesting."""
        video = TikTokVideo.from_apify(item())
        assert video.cover_url is not None
        assert "cover.jpeg" in video.cover_url

    def test_falls_back_to_the_original_cover(self) -> None:
        meta = {**REAL_ITEM["videoMeta"], "coverUrl": None}
        video = TikTokVideo.from_apify(item(videoMeta=meta))
        assert video.cover_url is not None
        assert "orig.jpeg" in video.cover_url

    def test_normalises_hashtags_to_lowercase_names(self) -> None:
        """Apify returns objects; we want a flat lowercase list."""
        video = TikTokVideo.from_apify(item())
        assert video.hashtags == ["kenyansingulf", "sidehustles", "sandalsforwomen"]

    def test_survives_an_empty_caption(self) -> None:
        """1 of 10 real posts had no caption at all."""
        video = TikTokVideo.from_apify(item(text=None))
        assert video.caption is None
        assert video.has_caption_text is False

    def test_treats_a_null_metric_as_zero(self) -> None:
        video = TikTokVideo.from_apify(item(playCount=None))
        assert video.views == 0

    def test_ignores_fields_we_do_not_use(self) -> None:
        """Apify sends 28 keys; a new one must never break ingestion."""
        video = TikTokVideo.from_apify(item(someBrandNewFieldApifyAdded="surprise"))
        assert video.video_id == "7671596309060570375"

    def test_download_url_is_absent_unless_video_download_ran(self) -> None:
        assert TikTokVideo.from_apify(item()).download_url is None

    def test_download_url_is_captured_when_present(self) -> None:
        video = TikTokVideo.from_apify(
            item(mediaUrls=["https://api.apify.com/v2/key-value-stores/x/records/v.mp4"])
        )
        assert video.download_url is not None
        assert "api.apify.com" in video.download_url


class TestTikTokAuthorParsing:
    def test_parses_a_real_author(self) -> None:
        author = TikTokAuthor.model_validate(REAL_AUTHOR)
        assert author.handle == "zumamitumbabales"
        assert author.display_name == "ZUMA MITUMBA BALES"
        assert author.follower_count == 22000
        assert author.video_count == 1453

    def test_normalises_the_handle(self) -> None:
        author = TikTokAuthor.model_validate({**REAL_AUTHOR, "name": "@ZumaMitumbaBales"})
        assert author.handle == "zumamitumbabales"

    def test_keeps_the_bio_because_it_carries_phone_numbers(self) -> None:
        """Onboarding pre-fills the WhatsApp number from here."""
        author = TikTokAuthor.model_validate(REAL_AUTHOR)
        assert author.bio is not None
        assert "0105515839" in author.bio


# ── Engine behaviour ─────────────────────────────────────────────────────────


class TestFetchProfile:
    def test_returns_a_validated_profile(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: FakeResponse(json_data=[item(), item(id="7671596309060570376")]),
        )
        profile = engine.fetch_profile("zumamitumbabales")

        assert profile.author.handle == "zumamitumbabales"
        assert profile.video_count == 2

    def test_strips_the_at_sign_from_the_handle(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_post(*_args: Any, **kwargs: Any) -> FakeResponse:
            captured.update(kwargs.get("json") or {})
            return FakeResponse(json_data=[item()])

        monkeypatch.setattr(httpx, "post", fake_post)
        engine.fetch_profile("@ZumaMitumbaBales")

        assert captured["profiles"] == ["zumamitumbabales"]

    def test_caps_the_import_by_default(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The spike account had 1,453 videos. Importing all of it costs real money
        for mostly stale stock, so the default must be a recent-N cap.
        """
        captured: dict[str, Any] = {}

        def fake_post(*_args: Any, **kwargs: Any) -> FakeResponse:
            captured.update(kwargs.get("json") or {})
            return FakeResponse(json_data=[item()])

        monkeypatch.setattr(httpx, "post", fake_post)
        engine.fetch_profile("zumamitumbabales")

        assert captured["resultsPerPage"] == 30

    def test_never_asks_apify_to_download_covers(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """We store our own copies; paying the actor to do it too is waste."""
        captured: dict[str, Any] = {}

        def fake_post(*_args: Any, **kwargs: Any) -> FakeResponse:
            captured.update(kwargs.get("json") or {})
            return FakeResponse(json_data=[item()])

        monkeypatch.setattr(httpx, "post", fake_post)
        engine.fetch_profile("zumamitumbabales")

        assert captured["shouldDownloadCovers"] is False

    def test_an_empty_result_raises_a_readable_error(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Nothing at all means the handle is wrong or the account is hidden.

        Distinct from an account that EXISTS but has no posts — see
        TestProfilesWithNoPosts. That one returns a row and must succeed.
        """
        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(json_data=[]))

        with pytest.raises(ScraperError, match="handle is spelled correctly"):
            engine.fetch_profile("nobody")

    def test_a_provider_error_carries_its_message(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare status code is not enough to debug an actor."""
        monkeypatch.setattr(
            httpx,
            "post",
            lambda *a, **k: FakeResponse(status_code=402, text="Monthly usage exceeded"),
        )

        with pytest.raises(ScraperError, match="Monthly usage exceeded"):
            engine.fetch_profile("zumamitumbabales")

    def test_a_transport_failure_is_wrapped_not_leaked(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_args: Any, **_kwargs: Any) -> FakeResponse:
            raise httpx.ConnectTimeout("timed out")

        monkeypatch.setattr(httpx, "post", boom)

        with pytest.raises(ScraperError, match="Could not reach Apify"):
            engine.fetch_profile("zumamitumbabales")

    def test_a_changed_payload_shape_fails_loudly(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The failure mode this border exists to prevent: the actor changes, and
        we notice at the edge instead of storing a catalogue of empty products.

        NOTE ON THE PAYLOAD. This test used to assert on a row with no ``id``
        at all. That turned out to be what Apify returns for a real account
        with no posts, so it failed a seller connecting a fresh TikTok on
        2026-08-19. A shape change is now a row that IS a post — it has an id —
        whose fields no longer parse.
        """
        broken = item(playCount="not-a-number")
        monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResponse(json_data=[broken]))

        with pytest.raises(ScraperError, match="did not match the expected shape"):
            engine.fetch_profile("zumamitumbabales")


class TestFetchVideo:
    def test_single_link_ingestion_returns_the_same_type(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both ingestion paths share a return type, so they share downstream code."""
        captured: dict[str, Any] = {}

        def fake_post(*_args: Any, **kwargs: Any) -> FakeResponse:
            captured.update(kwargs.get("json") or {})
            return FakeResponse(json_data=[item()])

        monkeypatch.setattr(httpx, "post", fake_post)
        profile = engine.fetch_video("https://www.tiktok.com/@x/video/123")

        assert profile.video_count == 1
        assert "postURLs" in captured


class TestDownloadMedia:
    def test_adds_the_token_for_apify_hosted_media(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Apify's key-value store is private and 403s without the token. A spike
        once fetched the 403 body and passed it to Gemini as a video.
        """
        captured: dict[str, Any] = {}

        def fake_get(_url: str, **kwargs: Any) -> FakeResponse:
            captured["params"] = kwargs.get("params")
            return FakeResponse(content=b"x" * 50_000, content_type="video/mp4")

        monkeypatch.setattr(httpx, "get", fake_get)
        engine.download_media(
            "https://api.apify.com/v2/key-value-stores/x/records/v.mp4", expect="video"
        )

        assert captured["params"] == {"token": "test-token"}

    def test_does_not_leak_the_token_to_other_hosts(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def fake_get(_url: str, **kwargs: Any) -> FakeResponse:
            captured["params"] = kwargs.get("params")
            return FakeResponse(content=b"x" * 50_000, content_type="image/jpeg")

        monkeypatch.setattr(httpx, "get", fake_get)
        engine.download_media("https://p19.tiktokcdn-us.com/cover.jpeg", expect="image")

        assert captured["params"] is None

    def test_refuses_a_wrong_content_type(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact bug that spent a paid Gemini call on a JSON error body."""
        monkeypatch.setattr(
            httpx,
            "get",
            lambda *a, **k: FakeResponse(
                content=b'{"error": "insufficient-permissions"}',
                content_type="application/json",
                text='{"error": "insufficient-permissions"}',
            ),
        )

        with pytest.raises(ScraperError, match="Expected video"):
            engine.download_media("https://api.apify.com/x.mp4", expect="video")

    def test_refuses_implausibly_small_media(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            httpx, "get", lambda *a, **k: FakeResponse(content=b"tiny", content_type="video/mp4")
        )

        with pytest.raises(ScraperError, match="implausibly small"):
            engine.download_media("https://api.apify.com/x.mp4", expect="video")

    def test_returns_bytes_for_valid_media(
        self, engine: ApifyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            httpx,
            "get",
            lambda *a, **k: FakeResponse(content=b"y" * 60_000, content_type="video/mp4"),
        )

        assert len(engine.download_media("https://api.apify.com/x.mp4", expect="video")) == 60_000


class TestProfilesWithNoPosts:
    """
    A brand-new account is an ordinary state, not a broken payload.

    Apify returns ONE row for a profile with nothing to show: it carries
    ``authorMeta`` and no video fields at all. Parsing that row as a video
    raised "the actor may have changed" and blocked verification entirely —
    reported from the field on 2026-08-19 against a freshly created account.

    Bio-code verification reads the bio and never touches the video list, so a
    seller with zero posts must still be able to prove they own the account.
    """

    def author_row(self, **overrides: Any) -> dict[str, Any]:
        """The placeholder row: real authorMeta, no post id."""
        row: dict[str, Any] = {
            "authorMeta": {
                "id": "7612345678901234567",
                "name": "biasharahooks",
                "nickName": "Biashara Hooks",
                "signature": "bmall-C6N7B6",
                "fans": 0,
                "video": 0,
            },
            "text": None,
            "hashtags": [],
        }
        row.update(overrides)
        return row

    def test_a_profile_with_no_posts_parses(self) -> None:
        profile = ApifyEngine._to_profile([self.author_row()], context="@biasharahooks")

        assert profile.videos == []
        assert profile.author.handle == "biasharahooks"

    def test_the_bio_survives_so_verification_can_still_work(self) -> None:
        """The whole point: an empty account can still prove ownership."""
        profile = ApifyEngine._to_profile([self.author_row()], context="@biasharahooks")

        assert profile.author.bio == "bmall-C6N7B6"

    def test_no_items_at_all_is_still_an_error(self) -> None:
        """A wrong handle needs to be reported, and says what to check."""
        with pytest.raises(ScraperError, match="handle is spelled correctly"):
            ApifyEngine._to_profile([], context="@nobody")


class TestPartiallyBadPayloads:
    def test_one_unparseable_post_does_not_lose_the_others(self) -> None:
        """Twenty-nine good posts are worth more than a clean failure."""
        good = item(id="7100000000000000001")
        bad = item(id="7100000000000000002", playCount="not-a-number")

        profile = ApifyEngine._to_profile([good, bad], context="@seller")

        assert len(profile.videos) == 1
        assert profile.videos[0].video_id == "7100000000000000001"

    def test_a_dropped_post_is_recorded_rather_than_swallowed(self) -> None:
        good = item(id="7100000000000000001")
        bad = item(id="7100000000000000002", playCount="not-a-number")

        profile = ApifyEngine._to_profile([good, bad], context="@seller")

        assert len(profile.warnings) == 1
        assert "7100000000000000002" in profile.warnings[0]

    def test_every_post_failing_is_still_a_loud_error(self) -> None:
        """
        That is a shape change, not bad luck. Silently returning zero videos
        would turn a renamed field into a catalogue of empty products.
        """
        bad = item(id="7100000000000000001", playCount="not-a-number")

        with pytest.raises(ScraperError, match="actor may have changed"):
            ApifyEngine._to_profile([bad], context="@seller")
