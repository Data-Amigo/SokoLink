"""
TikTok ingestion, behind our own interface.

    handle ──> ScraperEngine ──> ScrapedProfile (validated)
                    │
                    └── ApifyEngine today; anything else tomorrow

WHY the adapter: Apify is a paid third party that can break, rate-limit, or be
outgrown. Callers depend on ``ScraperEngine``, never on Apify, so swapping the
engine is a change to this file and nowhere else. TIKTOK proved the value of
this seam by swapping vision providers three times through an equivalent one.

TWO HARD RULES, both learned by spending money:

1. **Cover URLs expire.** They are signed CDN links. We download and store our
   own copies, or the storefront fills with broken images within days.
2. **Apify-hosted video downloads need the API token.** ``mediaUrls`` points
   into Apify's key-value store and 403s without it. A spike once fetched a
   214-byte JSON error and passed it to Gemini *as a video* — so this module
   refuses any response whose content type is not what was asked for.

COST: roughly $0.30 per 1,000 posts. Every call here is billable. The
once-per-day cache guard lives in the calling service, not here — this module's
job is to fetch and validate, not to decide whether fetching is wise.
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from app.config import settings
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor, TikTokVideo

#: Cap on an initial profile import.
#:
#: Real sellers have deep back-catalogues — the spike account had 1,453 videos,
#: which is ~$0.44 of Apify for one seller, most of it stale stock nobody will
#: buy. Recent posts are the ones with live inventory behind them.
DEFAULT_PROFILE_LIMIT = 30

#: Apify boots the actor before it scrapes; a cold start can take minutes.
RUN_TIMEOUT_SECONDS = 600.0

#: Anything smaller than this is an error page, not media.
MIN_MEDIA_BYTES = 10_000


class ScraperError(RuntimeError):
    """
    Ingestion failed in a way the caller must handle.

    Carries the provider's own message: a bare status code is not enough to
    debug an actor, and this text reaches the seller's dashboard as a retry
    prompt rather than being swallowed.
    """


class ScraperEngine(Protocol):
    """The contract every ingestion engine must satisfy."""

    def fetch_profile(self, handle: str, limit: int = DEFAULT_PROFILE_LIMIT) -> ScrapedProfile:
        """Fetch a seller's recent videos and profile."""
        ...

    def fetch_video(self, url: str) -> ScrapedProfile:
        """Fetch one video by its TikTok URL."""
        ...

    def download_media(self, url: str, expect: str) -> bytes:
        """Download a cover or video, refusing anything of the wrong type."""
        ...


class ApifyEngine:
    """Ingestion via the Apify TikTok actor."""

    def __init__(self, token: str | None = None, actor_id: str | None = None) -> None:
        self._token = token or settings.require("apify_token")
        self._actor = actor_id or settings.apify_tiktok_actor_id

    # ── Public API ───────────────────────────────────────────────────────────

    def fetch_profile(self, handle: str, limit: int = DEFAULT_PROFILE_LIMIT) -> ScrapedProfile:
        """
        Fetch a seller's recent videos.

        Args:
            handle: TikTok handle, with or without the leading @.
            limit: Most recent N posts. Defaults to a deliberate cap — see
                DEFAULT_PROFILE_LIMIT.

        Returns:
            The seller's profile and validated videos.

        Raises:
            ScraperError: On any provider or validation failure.
        """
        clean = handle.strip().lstrip("@").lower()
        items = self._run_actor({"profiles": [clean], "resultsPerPage": limit})
        return self._to_profile(items, context=f"@{clean}")

    def fetch_video(self, url: str) -> ScrapedProfile:
        """
        Fetch a single video by URL — the paste-a-link ingestion path.

        Args:
            url: A TikTok video URL.

        Returns:
            A profile containing the one video, so both ingestion paths share
            a return type and therefore share all downstream code.

        Raises:
            ScraperError: On any provider or validation failure.
        """
        items = self._run_actor({"postURLs": [url.strip()], "resultsPerPage": 1})
        return self._to_profile(items, context=url)

    def fetch_profile_with_video(
        self, handle: str, limit: int = DEFAULT_PROFILE_LIMIT
    ) -> ScrapedProfile:
        """
        Fetch with downloadable video URLs attached, for cascade tier 3.

        Kept separate from :meth:`fetch_profile` because enabling video download
        makes the actor slower and more expensive. Tier 3 is reached only when
        cheaper tiers fail, so most syncs must never pay for this.

        Raises:
            ScraperError: On any provider or validation failure.
        """
        clean = handle.strip().lstrip("@").lower()
        items = self._run_actor(
            {"profiles": [clean], "resultsPerPage": limit, "shouldDownloadVideos": True}
        )
        return self._to_profile(items, context=f"@{clean}")

    def download_media(self, url: str, expect: str = "image") -> bytes:
        """
        Download a cover or video, refusing anything that is not media.

        Args:
            url: Cover URL, or an Apify key-value store media URL.
            expect: ``"image"`` or ``"video"`` — checked against the response's
                content type.

        Returns:
            The raw bytes.

        Raises:
            ScraperError: If the response is an error, the wrong content type,
                or implausibly small. Passing an error page on to a paid vision
                call is how money gets spent on nothing.
        """
        # Apify's key-value store is not public. Without the token this 403s
        # with a JSON body that is easy to mistake for media.
        params = {"token": self._token} if "api.apify.com" in url else None

        try:
            response = httpx.get(url, params=params, timeout=180.0, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise ScraperError(f"Media download failed for {url[:80]}: {exc}") from exc

        if response.status_code >= 400:
            raise ScraperError(
                f"Media download returned {response.status_code}: {response.text[:200]}"
            )

        content_type = response.headers.get("content-type", "")
        if expect not in content_type:
            raise ScraperError(
                f"Expected {expect} but got content-type {content_type!r} "
                f"({len(response.content)} bytes). Body: {response.text[:200]}"
            )

        if len(response.content) < MIN_MEDIA_BYTES:
            raise ScraperError(
                f"Media is implausibly small ({len(response.content)} bytes) — "
                "almost certainly an error page rather than {expect}."
            )

        return response.content

    # ── Internals ────────────────────────────────────────────────────────────

    def _run_actor(self, run_input: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Run the actor and return its dataset items.

        Covers and subtitles are never downloaded by the actor: we store our own
        covers, and spike 02 found subtitles are always absent for this content.

        Raises:
            ScraperError: On a transport failure or a non-2xx response.
        """
        payload = {
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
            "shouldDownloadSlideshowImages": False,
            **run_input,
        }

        try:
            response = httpx.post(
                f"https://api.apify.com/v2/acts/{self._actor}/run-sync-get-dataset-items",
                params={"token": self._token},
                json=payload,
                timeout=RUN_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise ScraperError(f"Could not reach Apify: {exc}") from exc

        if response.status_code >= 400:
            raise ScraperError(f"Apify returned {response.status_code}: {response.text[:500]}")

        try:
            items = response.json()
        except ValueError as exc:
            raise ScraperError(f"Apify returned non-JSON: {response.text[:200]}") from exc

        if not isinstance(items, list):
            raise ScraperError(f"Apify returned {type(items).__name__}, expected a list")

        return items

    @staticmethod
    def _to_profile(items: list[dict[str, Any]], context: str) -> ScrapedProfile:
        """
        Validate raw items into a ScrapedProfile.

        An empty result is NOT an error — a private or empty profile is a real
        state the seller needs told about, and raising here would make it
        indistinguishable from a broken scrape.

        Raises:
            ScraperError: If the payload is present but unparseable, which means
                the actor's shape changed and we must notice loudly.
        """
        if not items:
            raise ScraperError(
                f"No videos found for {context}. The profile may be private, "
                "empty, or the handle may be wrong."
            )

        author_data = items[0].get("authorMeta") or {}
        if not author_data:
            raise ScraperError(f"Apify returned items for {context} with no authorMeta")

        try:
            author = TikTokAuthor.model_validate(author_data)
            videos = [TikTokVideo.from_apify(item) for item in items]
        except Exception as exc:  # noqa: BLE001 — re-raised with context below
            raise ScraperError(
                f"Apify payload for {context} did not match the expected shape — "
                f"the actor may have changed: {exc}"
            ) from exc

        return ScrapedProfile(author=author, videos=videos)


def get_scraper() -> ScraperEngine:
    """
    The engine the application uses.

    A function rather than a module-level instance so nothing constructs a
    client — or demands an API token — merely by importing this module.
    """
    return ApifyEngine()
