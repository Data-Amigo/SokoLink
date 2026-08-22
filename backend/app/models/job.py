"""
One unit of background work.

    route ──▶ enqueue Job (queued) ──▶ returns immediately
                    │
              worker claims it
                    │
              running ──▶ succeeded | failed
                    │
              HTMX polls /jobs/{id}

WHY A QUEUE AT ALL. Every external call in this product is slow, costs money,
or both — an Apify profile scrape takes thirty seconds to three minutes, a
Gemini video call up to a minute, a CapCut export several. Running those inside
an HTTP request is not slow, it is a **billing bug**: the request times out, the
proxy returns 502, the seller presses the button again, and press two pays for
press one's work a second time.

WHY POSTGRES AND NOT REDIS. This table plus ``SELECT … FOR UPDATE SKIP LOCKED``
is a queue, and it is a queue we already operate, back up and monitor. At
hundreds of jobs a day a broker would be a second datastore to run and a second
thing to be down, bought with throughput we will never use.

WHY RETRIES ARE OFF BY DEFAULT. ``max_attempts`` is 1 unless a job kind asks
for more. Nearly everything here spends money on a third party, and a queue
that silently retries a paid call is the classic way to turn one outage into a
bill. A failed job stops and says so; a human decides whether it was worth
paying for twice.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import JobStatus

if TYPE_CHECKING:
    from app.models.seller import Seller


class Job(Base):
    """A piece of work to be done outside the request that asked for it."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    #: What to run. Maps to a handler in ``app/worker.py``.
    kind: Mapped[str] = mapped_column(String(50), nullable=False)

    #: Arguments, as JSON. Ids and scalars only — never a whole object, because
    #: the row may sit in the queue long enough for the object to have changed,
    #: and the handler must read the current state, not a snapshot of it.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: Whose work this is. Nullable for system jobs that belong to nobody.
    #: Carries the cost attribution the spend ledger will need in step 6.
    seller_id: Mapped[int | None] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"))
    seller: Mapped[Seller | None] = relationship(back_populates="jobs")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JobStatus.QUEUED.value)

    #: Collapses duplicate work while it is still pending.
    #:
    #: A seller who presses Sync twice must not buy two scrapes. Enforced by a
    #: PARTIAL unique index — unique only among queued and running rows, so the
    #: same key can legitimately be used again once the first one finishes.
    dedupe_key: Mapped[str | None] = mapped_column(String(120))

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: 1 means "do not retry", which is the default on purpose — see the module
    #: docstring. A kind that is free and idempotent may raise it.
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    #: When this becomes eligible to run. Lets a job be delayed or backed off
    #: without a separate scheduler.
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    #: Whatever the handler returned, for the UI to render. Small by
    #: convention — counts and ids, not payloads.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    #: Safe to show the person who triggered the work.
    error: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_jobs_status_valid",
        ),
        CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
        CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_at_least_one"),
        # The claim query: oldest eligible job first.
        Index("ix_jobs_claimable", "status", "scheduled_for"),
        # The dedupe guard. PARTIAL, so a key is reusable once its job is done.
        Index(
            "uq_jobs_dedupe_pending",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
        ),
    )

    @property
    def status_enum(self) -> JobStatus:
        return JobStatus(self.status)

    @property
    def is_finished(self) -> bool:
        return self.status_enum.is_final

    @property
    def can_retry(self) -> bool:
        """Whether a failure is worth trying again automatically."""
        return self.attempts < self.max_attempts

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.kind} {self.status}>"
