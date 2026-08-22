"""
The validation border between Apify and everything downstream.

    Apify JSON ──> TikTokVideo / TikTokAuthor ──> services ──> database
                   (validated here, once)

WHY a border at all: Apify's response is a third-party contract we do not
control. Actors get updated, fields get renamed, and a scraper that silently
half-works produces a catalogue full of empty products. Validating once, here,
means a shape change fails loudly at the edge with a readable error instead of
surfacing three layers later as a None.

The models are deliberately TOLERANT of extra fields and STRICT about the ones
we depend on. Apify returns 28 top-level keys; we need nine. Ignoring the rest
means an added field never breaks us, while a removed one we rely on does —
which is exactly the right way round.

Field names come from a real payload (spike 01, @zumamitumbabales), not from
documentation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TikTokAuthor(BaseModel):
    """
    A seller's TikTok profile, from Apify's ``authorMeta``.

    Everything here auto-fills onboarding so the seller confirms rather than
    types. Spike 01 confirmed all of it is present for a real account.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    handle: str = Field(alias="name")
    display_name: str | None = Field(default=None, alias="nickName")
    avatar_url: str | None = Field(default=None, alias="avatar")

    #: The profile bio. Kenyan sellers routinely put phone numbers and shop
    #: addresses here — spike 01 found three numbers in this field alone, which
    #: is why onboarding can pre-fill the WhatsApp number.
    bio: str | None = Field(default=None, alias="signature")

    follower_count: int = Field(default=0, alias="fans")
    video_count: int = Field(default=0, alias="video")

    #: Lifetime likes across the whole account, from ``authorMeta.heart``.
    #: The smoothest account-level trend line there is — it only ever rises,
    #: so a dip means we failed to scrape, not that the creator declined.
    total_likes: int = Field(default=0, alias="heart")
    verified: bool = False
    private_account: bool = Field(default=False, alias="privateAccount")

    @field_validator("handle")
    @classmethod
    def _normalise_handle(cls, v: str) -> str:
        """Lowercase and strip @, so one seller cannot exist twice."""
        return v.strip().lstrip("@").lower()

    @field_validator("follower_count", "video_count", "total_likes", mode="before")
    @classmethod
    def _coerce_count(cls, v: Any) -> int:
        """Apify occasionally returns null for a count; treat it as zero."""
        return int(v) if v is not None else 0


class TikTokVideo(BaseModel):
    """
    One scraped video, from an Apify dataset item.

    Only the fields the catalogue actually uses. Everything else in the payload
    is ignored on purpose — see the module docstring.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    video_id: str = Field(alias="id")

    #: The caption. Spike 01: hashtag soup, and 0/10 contained a price. Kept
    #: verbatim as ground truth and as replay input when a prompt changes —
    #: never as a price source.
    caption: str | None = Field(default=None, alias="text")

    video_url: str | None = Field(default=None, alias="webVideoUrl")

    #: Signed CDN URL that EXPIRES. Must be downloaded and stored by us, or the
    #: storefront fills with broken images within days.
    cover_url: str | None = None

    #: Present only when the actor ran with video download enabled, and it
    #: points into Apify's key-value store — it 403s without the API token.
    #: Learned by sending a 214-byte JSON error to Gemini as a video.
    download_url: str | None = None

    duration_seconds: int | None = None

    hashtags: list[str] = Field(default_factory=list)

    #: When the creator posted it. WITHOUT THIS THERE IS NO TIME AXIS — no
    #: "your last ten posts", no chart, no "you posted less this week".
    #: Apify supplies `createTimeISO`; verified against a real payload
    #: (spike 01) rather than taken from documentation.
    posted_at: datetime | None = Field(default=None, alias="createTimeISO")

    #: Pinned posts sit at the top of a profile accumulating views for months.
    #: Left in an average they make every new post look like a failure, so the
    #: flag has to survive to the database for the average to exclude them.
    is_pinned: bool = Field(default=False, alias="isPinned")

    views: int = Field(default=0, alias="playCount")
    likes: int = Field(default=0, alias="diggCount")
    comments: int = Field(default=0, alias="commentCount")
    shares: int = Field(default=0, alias="shareCount")

    #: Saves. The strongest purchase-intent signal TikTok exposes — someone
    #: bookmarking a bale to come back to it is much closer to buying than
    #: someone liking it.
    saves: int = Field(default=0, alias="collectCount")

    reposts: int = Field(default=0, alias="repostCount")

    @field_validator("views", "likes", "comments", "shares", "saves", "reposts", mode="before")
    @classmethod
    def _coerce_metric(cls, v: Any) -> int:
        return int(v) if v is not None else 0

    @field_validator("posted_at", mode="before")
    @classmethod
    def _parse_posted_at(cls, v: Any) -> Any:
        """
        Accept Apify's ISO string, and tolerate its absence.

        A post with no timestamp is still worth storing — it simply cannot
        appear on a time axis — so this returns None rather than raising.
        """
        if v in (None, ""):
            return None
        return v

    @classmethod
    def from_apify(cls, item: dict[str, Any]) -> TikTokVideo:
        """
        Build from one raw Apify dataset item.

        Flattens the nested ``videoMeta`` and normalises ``hashtags`` from a
        list of objects to a list of names. Done here rather than in the model
        because aliases cannot reach into nested dictionaries, and hiding this
        in a service would spread payload knowledge across the codebase.

        Args:
            item: One dataset item exactly as Apify returned it.

        Returns:
            A validated video.

        Raises:
            pydantic.ValidationError: If a field we depend on is missing or the
                wrong type — which is the point of validating at the border.
        """
        meta = item.get("videoMeta") or {}
        media = item.get("mediaUrls") or []

        tags: list[str] = []
        for tag in item.get("hashtags") or []:
            name = tag.get("name") if isinstance(tag, dict) else tag
            if name:
                tags.append(str(name).lower())

        return cls.model_validate(
            {
                **item,
                "cover_url": meta.get("coverUrl") or meta.get("originalCoverUrl"),
                "duration_seconds": meta.get("duration"),
                "download_url": media[0] if media else None,
                "hashtags": tags,
            }
        )

    @property
    def has_caption_text(self) -> bool:
        """Whether the caption holds anything beyond whitespace."""
        return bool(self.caption and self.caption.strip())


class ScrapedProfile(BaseModel):
    """A profile scrape: the seller, plus the videos found."""

    model_config = ConfigDict(extra="ignore")

    author: TikTokAuthor

    #: May legitimately be EMPTY. A brand-new account, or one that has only
    #: private posts, is a real state — and the profile is still worth having,
    #: because bio-code verification reads the bio and nothing else.
    videos: list[TikTokVideo] = Field(default_factory=list)

    #: Individual posts that could not be parsed, with the reason.
    #:
    #: Mirrors ``SyncResult.warnings`` rather than inventing a second channel:
    #: one unparseable post must not cost us the other twenty-nine, but it must
    #: not vanish silently either. Ingestion folds these into its own warnings.
    warnings: list[str] = Field(default_factory=list)

    @property
    def video_count(self) -> int:
        return len(self.videos)
