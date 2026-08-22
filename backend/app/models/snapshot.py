"""
Metric history — the only thing here that cannot be recovered later.

    every sync ──┬──▶ PostMetricSnapshot      one row per post per DAY
                 └──▶ AccountMetricSnapshot   one row per account per DAY

WHY THESE TABLES EXIST, and why they were urgent.

``Post.views`` is overwritten on every sync. That is correct for rendering a
list, and useless for the question a creator actually asks: *am I growing?*
Nothing in a table of current values can answer it.

**This history cannot be backfilled.** TikTok will not tell us what a post had
last Tuesday, and Apify only ever returns today. Every day the product ran
without these tables was a day of data gone permanently — which is why this
migration shipped before the page that displays it.

WHY ONE ROW PER DAY, not one per sync. TikTok's counters move slowly, and the
sync cooldown is already 24 hours — but ``force=True`` bypasses it, and a
seller pressing Refresh four times would otherwise write four near-identical
rows an hour apart. That is not extra signal; it is a chart with noise in it.
The unique key is (subject, captured_on), and a repeat sync UPDATES the day's
row rather than appending. Last write wins, which is also the freshest.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

if TYPE_CHECKING:
    from app.models.post import Post
    from app.models.social_account import SocialAccount


class PostMetricSnapshot(Base):
    """What one post's numbers were on one day."""

    __tablename__ = "post_metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    post: Mapped[Post] = relationship(back_populates="snapshots")

    #: The DAY this measurement belongs to, and half of the unique key. A date
    #: rather than a timestamp because that is the resolution the question is
    #: asked at, and storing finer granularity would invite writing it.
    captured_on: Mapped[date] = mapped_column(Date, nullable=False)

    #: The exact moment of the most recent write for that day, kept for
    #: debugging — "was this taken before or after they posted?"
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saves: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reposts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("post_id", "captured_on", name="uq_post_snapshot_per_day"),
        CheckConstraint(
            "views >= 0 AND likes >= 0 AND comments >= 0 "
            "AND shares >= 0 AND saves >= 0 AND reposts >= 0",
            name="ck_post_snapshots_non_negative",
        ),
    )

    def __repr__(self) -> str:
        return f"<PostMetricSnapshot post={self.post_id} on={self.captured_on} views={self.views}>"


class AccountMetricSnapshot(Base):
    """What one account's totals were on one day."""

    __tablename__ = "account_metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    social_account_id: Mapped[int] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False
    )
    social_account: Mapped[SocialAccount] = relationship(back_populates="snapshots")

    captured_on: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    follower_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    post_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Lifetime likes across the account — Apify's ``authorMeta.heart``. The
    #: smoothest of the account-level trend lines, because it only ever rises.
    total_likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("social_account_id", "captured_on", name="uq_account_snapshot_per_day"),
        CheckConstraint(
            "follower_count >= 0 AND post_count >= 0 AND total_likes >= 0",
            name="ck_account_snapshots_non_negative",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AccountMetricSnapshot account={self.social_account_id} "
            f"on={self.captured_on} followers={self.follower_count}>"
        )
