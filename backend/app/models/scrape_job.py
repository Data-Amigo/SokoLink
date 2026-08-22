"""
ScrapeJob — a record of one ingestion run, and the cache that keeps costs sane.

    request ──> is there a succeeded job for this seller in the last 24h?
                    │                              │
                   yes                             no
                    │                              │
              reuse payload                  call Apify, store payload

WHY this table exists at all: Apify bills roughly $0.30 per 1,000 posts, and
that cost scales directly with seller count. Scraping per request would make the
unit economics fail at exactly the moment the product succeeds. The raw payload
is kept so a parsing change can be replayed against real data without paying to
fetch it again.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ScrapeStatus

if TYPE_CHECKING:
    from app.models.seller import Seller


class ScrapeJob(Base):
    """One attempt to pull content for a seller."""

    __tablename__ = "scrape_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), nullable=False
    )
    seller: Mapped[Seller] = relationship(back_populates="scrape_jobs")

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ScrapeStatus.PENDING.value
    )

    #: Which platform was scraped.
    platform: Mapped[str] = mapped_column(String(20), nullable=False)

    #: Which ingestion path triggered this — a full profile pull or one link.
    ingest_method: Mapped[str] = mapped_column(String(20), nullable=False)

    #: The raw, unmodified provider response. Kept deliberately: it is what lets
    #: us re-run a changed parser against real data for free, and it is the
    #: evidence when a seller disputes what we extracted.
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: Populated when status is FAILED. Surfaced to the seller as a retry
    #: prompt, never swallowed — a silent scrape failure looks identical to
    #: "you have no videos", which sends sellers away thinking we are broken.
    error: Mapped[str | None] = mapped_column(Text)

    #: Videos returned. Zero on a *succeeded* job usually means a private or
    #: empty profile, not a bug — worth distinguishing in the UI.
    video_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Products actually created or updated from this run. Differs from
    #: video_count when items were skipped as duplicates or non-products.
    products_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_scrape_jobs_status_valid",
        ),
        CheckConstraint(
            "platform IN ('tiktok', 'instagram', 'facebook', 'jumia')",
            name="ck_scrape_jobs_platform_valid",
        ),
        CheckConstraint(
            "ingest_method IN ('profile_sync', 'single_link')",
            name="ck_scrape_jobs_ingest_method_valid",
        ),
        # A failed job without a reason is unactionable for whoever debugs it.
        CheckConstraint(
            "status <> 'failed' OR error IS NOT NULL",
            name="ck_scrape_jobs_failed_needs_error",
        ),
        CheckConstraint(
            "video_count >= 0 AND products_upserted >= 0",
            name="ck_scrape_jobs_counts_non_negative",
        ),
        # The once-per-day guard runs this lookup before every Apify call, so
        # it must be indexed: "latest job for this seller, newest first".
        Index("ix_scrape_jobs_seller_started", "seller_id", "started_at"),
    )

    @property
    def is_terminal(self) -> bool:
        """Whether this job has finished, one way or the other."""
        return self.status in (ScrapeStatus.SUCCEEDED.value, ScrapeStatus.FAILED.value)

    def __repr__(self) -> str:
        return f"<ScrapeJob id={self.id} seller={self.seller_id} status={self.status}>"
