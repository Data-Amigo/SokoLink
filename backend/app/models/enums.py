"""
Shared enumerations.

Stored as strings in Postgres rather than native enum types. Adding a value to a
native PG enum needs a migration and an exclusive lock; adding one here is a
code change. For values that will grow — platforms especially — that difference
matters far more than the few bytes saved.

Validity is enforced by a CHECK constraint on each column, so the database still
refuses a value the application never defined.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    """
    Where a product or an account came from.

    Deliberately generalised before it was needed. The model was TikTok-shaped
    (`tiktok_handle`, `tiktok_video_id`) until 2026-08-08, when multi-channel
    entered the plan. Generalising with zero production rows is a migration
    nobody notices; generalising after fifty sellers have live catalogues means
    backfilling real shops.

    Only TIKTOK has an ingestion engine today. The rest are declared so the
    schema, the URLs and the UI never have to change to admit them.
    """

    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    JUMIA = "jumia"

    #: Not a social platform — the seller uploaded this themselves.
    MANUAL = "manual"

    @property
    def is_social(self) -> bool:
        """Whether this platform can be connected and synced."""
        return self is not Platform.MANUAL

    @property
    def label(self) -> str:
        """Display name, for the dashboard."""
        return {
            Platform.TIKTOK: "TikTok",
            Platform.INSTAGRAM: "Instagram",
            Platform.FACEBOOK: "Facebook",
            Platform.JUMIA: "Jumia",
            Platform.MANUAL: "Uploaded",
        }[self]


class IngestMethod(StrEnum):
    """
    How a product entered the catalogue.

    Separate from :class:`Platform` because they answer different questions.
    Platform says *where it came from*; this says *how it got here* — and it is
    the second that decides what a re-sync may touch.
    """

    PROFILE_SYNC = "profile_sync"
    """Bulk-imported from a connected account. A re-sync owns and may update these."""

    SINGLE_LINK = "single_link"
    """Added from one pasted post URL. Not owned by any sync."""

    UPLOAD = "upload"
    """Uploaded by the seller. A sync must NEVER touch these."""

    @property
    def is_sync_owned(self) -> bool:
        """
        Whether a profile re-sync is allowed to modify or remove this product.

        Only PROFILE_SYNC items. A seller who adds stock by hand, syncs their
        feed, and watches it vanish does not come back — so this is a rail with
        its own test, not a convention.
        """
        return self is IngestMethod.PROFILE_SYNC


class ProductStatus(StrEnum):
    """
    Where a product sits between arriving and being buyable.

    DRAFT is the default for everything, including AI output. Publishing is a
    deliberate human act that requires a price.
    """

    DRAFT = "draft"
    """Arrived, not yet confirmed by the seller. Invisible to buyers."""

    PUBLISHED = "published"
    """Live on the storefront. Requires a price."""

    ARCHIVED = "archived"
    """Withdrawn by the seller. Kept for order history, hidden from the shop."""


class ScrapeStatus(StrEnum):
    """Lifecycle of one ingestion run."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PriceSource(StrEnum):
    """
    Which tier of the extraction cascade produced the price.

    Recorded per product because it is the feedback signal for the whole
    approach: if the video tier rarely fires, the expensive path is not earning
    its cost. Without this column that question can only be guessed at.
    """

    CAPTION = "caption"
    """Read from caption text. Near-free, and vanishingly rare in practice."""

    COVER_IMAGE = "cover_image"
    """Read from text printed on the cover or an uploaded photo."""

    VIDEO = "video"
    """Heard or seen in the clip itself. Highest yield, highest cost."""

    SELLER = "seller"
    """Typed by a human. Always outranks anything a model produced."""
