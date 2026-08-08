"""
Shared enumerations.

Stored as strings in Postgres rather than native enum types. Adding a value to a
native PG enum needs a migration and an exclusive lock; adding one here is a
code change. For values that will grow — categories especially — that difference
matters more than the few bytes saved.

Validity is enforced by a CHECK constraint on each column, so the database still
refuses a value the application never defined.
"""

from __future__ import annotations

from enum import StrEnum


class ProductSource(StrEnum):
    """
    How a product entered the catalogue.

    Provenance is not decoration — it decides what a re-sync may touch. A
    profile sync owns the products it created and may update them; it must never
    modify or remove one the seller added by hand.
    """

    TIKTOK_PROFILE = "tiktok_profile"
    """Bulk-imported from a seller's TikTok feed."""

    TIKTOK_LINK = "tiktok_link"
    """Added from a single pasted TikTok URL."""

    MANUAL = "manual"
    """Uploaded by the seller. Never has a TikTok video behind it."""


class ProductStatus(StrEnum):
    """
    Where a product sits between arriving and being buyable.

    DRAFT is the default for everything, including AI output. Publishing is a
    deliberate human act that requires a price — see the CHECK constraint on
    Product.
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
    approach: if tier 3 rarely fires, the expensive path is not earning its
    cost; if tier 1 never fires, the caption tier can be dropped entirely.
    Without this column that question can only be guessed at.
    """

    CAPTION = "caption"
    """Tier 1 — read from caption text. Near-free, rare in practice."""

    COVER_IMAGE = "cover_image"
    """Tier 2 — read from text printed on the cover or uploaded photo."""

    VIDEO = "video"
    """Tier 3 — heard or seen in the clip itself. Highest yield, highest cost."""

    SELLER = "seller"
    """Typed by a human. Always outranks anything the model produced."""
