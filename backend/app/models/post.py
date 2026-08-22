"""
A post — content, not commerce.

    SocialAccount ──▶ Post ──┬──▶ PostMetricSnapshot   (history)
                             │
                             └──▶ Product              (0 or 1, optional)

WHY THIS TABLE EXISTS SEPARATELY FROM ``Product``.

``Product`` was "a post plus commerce fields", which broke in two directions
the moment analytics became the product:

  **It threw content away.** Ingestion skipped any post the model judged not a
  sellable item — right for a catalogue, wrong for analytics. A creator's
  face-to-camera video still has views, and views are the thing they are paying
  us to explain. Their chart would have had holes in it.

  **It made every creator a shopkeeper.** Someone who signs up for content
  tools and never sells anything would have had their videos stored as
  "products", and every storefront query would need to remember to exclude them.

So: ``Post`` is every post. ``Product`` is the subset someone decided to sell,
and it points here. A manual photo upload has a Product and no Post; a
talking-head video has a Post and no Product.

WHAT THE METRIC COLUMNS HERE ARE FOR. They hold the LATEST values only, so a
post list renders without touching the snapshot table. The history — and
therefore any answer to "am I growing?" — lives in ``PostMetricSnapshot``.
Overwriting these is fine precisely because the snapshot preserves what they
were.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.product import Product
    from app.models.snapshot import PostMetricSnapshot
    from app.models.social_account import SocialAccount


class Post(Base):
    """One piece of content published on a connected account."""

    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    social_account_id: Mapped[int] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False
    )
    social_account: Mapped[SocialAccount] = relationship(back_populates="posts")

    #: Denormalised from the account so post queries never need the join. A post
    #: cannot change platform, so this cannot drift.
    platform: Mapped[str] = mapped_column(String(20), nullable=False)

    #: The platform's own id for this post.
    platform_post_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Content ──────────────────────────────────────────────────────────────

    caption: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)

    post_url: Mapped[str | None] = mapped_column(String(500))

    #: OUR stored copy, relative to MEDIA_ROOT. Platform cover URLs are signed
    #: and expire within days — see services/media.py.
    cover_url: Mapped[str | None] = mapped_column(String(500))

    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    #: When the creator published it, per the platform. Nullable because a
    #: payload without it is still worth storing; it simply cannot be plotted.
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: Pinned posts accumulate views for months at the top of a profile.
    #: Averages must exclude them or every new post looks like a failure.
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Latest metrics ───────────────────────────────────────────────────────
    #
    # Overwritten on every sync, ON PURPOSE. The history is in the snapshots;
    # these exist so a list of fifty posts is one query.

    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saves: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ── Bookkeeping ──────────────────────────────────────────────────────────

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    snapshots: Mapped[list[PostMetricSnapshot]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True
    )

    #: The commerce record, if the seller chose to sell this post. Optional in
    #: both directions: most posts never become products.
    product: Mapped[Product | None] = relationship(back_populates="post")

    __table_args__ = (
        # Per platform, not global: two platforms can legitimately mint the
        # same numeric id, and a global unique would reject the second.
        UniqueConstraint("platform", "platform_post_id", name="uq_posts_platform_post"),
        CheckConstraint(
            "platform IN ('tiktok', 'instagram', 'facebook', 'jumia', 'manual')",
            name="ck_posts_platform_valid",
        ),
        CheckConstraint(
            "views >= 0 AND likes >= 0 AND comments >= 0 "
            "AND shares >= 0 AND saves >= 0 AND reposts >= 0",
            name="ck_posts_metrics_non_negative",
        ),
        # The analytics page's main query: this account's posts, newest first.
        Index("ix_posts_account_posted", "social_account_id", "posted_at"),
    )

    @property
    def engagement(self) -> int:
        """
        Total interactions, however a viewer chose to interact.

        Summed rather than ranked because the alternative is arguing about
        whether a save is worth three likes — a judgement that belongs to
        whoever reads the number, not to the schema.
        """
        return self.likes + self.comments + self.shares + self.saves + self.reposts

    @property
    def engagement_rate(self) -> float | None:
        """
        Engagement as a share of views, or None when nobody has seen it.

        None rather than 0.0: a post with no views has an UNKNOWN rate, and
        rendering that as 0% tells the creator something false.
        """
        if not self.views:
            return None
        return self.engagement / self.views

    def __repr__(self) -> str:
        return f"<Post {self.platform}:{self.platform_post_id} views={self.views}>"
