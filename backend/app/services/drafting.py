"""
The price cascade — deciding how much to spend on each video.

    caption ──miss──> cover image ──miss──> video
     free               cheap                expensive
        │                  │                     │
        └── confident price? ──> STOP ───────────┘

WHY this lives in a service rather than the agent: the agent knows how to ask a
model; this knows how much we are willing to pay to find out. Those are
different concerns, and conflating them is how a cost control ends up buried
inside a prompt.

THE ECONOMICS ARE THE DESIGN. Tier 3 is the only tier that worked for
@zumamitumbabales (3/3, versus 0/10 and 0/4 above it), and it is also the one
that costs real money per clip. Escalating only on failure is what makes both
facts survivable.

Every escalation is recorded on the result, so we can answer later whether the
expensive tier is earning its keep. Without that record the question is
unanswerable and the cost is unmanageable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.draft import DraftAgent, DraftAgentError
from app.models.enums import PriceSource
from app.schemas.draft import ProductDraft
from app.schemas.tiktok import TikTokVideo
from app.services.scraper import ScraperEngine, ScraperError


@dataclass(slots=True)
class CascadeResult:
    """
    What the cascade produced, and what it cost to get there.

    ``tiers_attempted`` exists so the expensive tier can be held to account: if
    tier 3 rarely changes the outcome, it should not be running. That question
    cannot be asked retrospectively unless it is recorded now.
    """

    draft: ProductDraft | None
    price_source: PriceSource | None
    tiers_attempted: list[str] = field(default_factory=list)

    #: Non-fatal failures. A tier that errored must not abort the cascade — the
    #: next tier may still succeed — but the failure is never swallowed either.
    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """Whether any usable draft came out, priced or not."""
        return self.draft is not None

    @property
    def has_price(self) -> bool:
        return self.draft is not None and self.draft.has_price

    @property
    def reached_video_tier(self) -> bool:
        """Whether the most expensive tier ran — the number to watch."""
        return "video" in self.tiers_attempted


class DraftingService:
    """Runs the cascade for one video."""

    def __init__(self, agent: DraftAgent, scraper: ScraperEngine) -> None:
        self._agent = agent
        self._scraper = scraper

    def draft_for_video(
        self, video: TikTokVideo, *, allow_video_tier: bool = True
    ) -> CascadeResult:
        """
        Produce the best draft available for one video, cheapest tier first.

        Args:
            video: A validated scraped video.
            allow_video_tier: Set False to forbid the expensive tier — used when
                a seller is over quota, or during a bulk import where paying for
                every clip would be ruinous.

        Returns:
            A CascadeResult. A result with no price is a legitimate outcome, not
            an error: the seller fills it in during review, which is exactly the
            human gate the whole design rests on.
        """
        result = CascadeResult(draft=None, price_source=None)

        # ── Tier 2: the cover image ─────────────────────────────────────────
        # Tier 1 (caption) is skipped as a PRICE source on purpose: measured at
        # 0/10 on real data, and every caption is already passed to the model as
        # hashtag context, so a separate text call would spend money to re-read
        # what tier 2 already sees.
        if video.cover_url:
            result.tiers_attempted.append("cover_image")
            try:
                image = self._scraper.download_media(video.cover_url, expect="image")
                draft = self._agent.draft_from_cover(video, image)
                result.draft = draft

                if draft.is_confident:
                    result.price_source = PriceSource.COVER_IMAGE
                    return result
            except (ScraperError, DraftAgentError) as exc:
                # Not fatal. The cover may have expired, or the model may have
                # hiccuped — the video tier can still rescue this item.
                result.warnings.append(f"cover tier failed: {exc}")

        # ── Tier 3: the video itself ────────────────────────────────────────
        if not allow_video_tier:
            result.warnings.append("video tier skipped: not allowed for this run")
            return result

        if not video.download_url:
            # Reaching tier 3 needs a scrape that enabled video download. The
            # caller has to opt into that cost explicitly, so its absence is a
            # normal state rather than a bug.
            result.warnings.append("video tier skipped: no download URL on this scrape")
            return result

        result.tiers_attempted.append("video")
        try:
            clip = self._scraper.download_media(video.download_url, expect="video")
            draft = self._agent.draft_from_video(video, clip)
        except (ScraperError, DraftAgentError) as exc:
            result.warnings.append(f"video tier failed: {exc}")
            return result

        # Only replace the cover-tier draft if this one is actually better.
        # A confident tier-2 draft would already have returned above, so the
        # incumbent here is weak — but "weak with a price" still beats
        # "confident about nothing".
        if result.draft is None or draft.has_price:
            result.draft = draft
            if draft.has_price:
                result.price_source = PriceSource.VIDEO

        return result
