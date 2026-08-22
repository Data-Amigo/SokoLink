"""
The queue: enqueueing work, and handing it to exactly one worker.

    enqueue()  ──▶ Job(queued)
                      │
    claim_next() ──▶ Job(running)   ← atomic, SKIP LOCKED
                      │
    complete() / fail()

THE ONE HARD PART IS ``claim_next``. Two workers polling the same table will
both see the same queued row. Reading then updating in two statements is a
classic race: both claim it, both run it, and a paid scrape is bought twice.

``SELECT … FOR UPDATE SKIP LOCKED`` inside the UPDATE makes the claim atomic —
the row is locked and marked running in one statement, and a second worker
hitting the same row *skips it* rather than blocking. That is what lets the
worker count go up without any coordination between them.

WHY NO BROKER. Postgres does this correctly at our volume, and it is a
datastore we already run, back up and monitor. Redis would be a second thing to
operate and a second thing to be down, bought with throughput we will not use.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import Job, JobStatus

#: How long a job may sit in ``running`` before we assume its worker died.
#:
#: Generous on purpose: a cold Apify actor can take minutes, and reclaiming a
#: job that is merely slow would run it twice — which for a paid call is the
#: thing the queue exists to prevent.
STALE_AFTER = timedelta(minutes=30)


class JobError(Exception):
    """Something is wrong with a job request, with a message fit to show."""


def enqueue(
    db: Session,
    kind: str,
    *,
    payload: dict[str, Any] | None = None,
    seller_id: int | None = None,
    dedupe_key: str | None = None,
    max_attempts: int = 1,
    delay: timedelta | None = None,
) -> Job | None:
    """
    Put work on the queue.

    Args:
        db: Session. The caller commits.
        kind: Which handler runs it. Must be registered in ``app/worker.py``.
        payload: Arguments as JSON. **Ids and scalars only** — the row may wait
            long enough that a copied object is stale by the time it runs.
        seller_id: Whose work it is, for attribution and the future budget.
        dedupe_key: If given, collapses with any queued or running job carrying
            the same key.
        max_attempts: Retries. Defaults to 1 — no retry — because nearly every
            job here spends money. See the model docstring.
        delay: Run no earlier than this far in the future.

    Returns:
        The new job, or **None** when an identical one is already pending. None
        is a success: the work the caller wanted is already going to happen.
    """
    scheduled_for = datetime.now(UTC) + (delay if delay is not None else timedelta())

    values = {
        "kind": kind,
        "payload": payload or {},
        "seller_id": seller_id,
        "dedupe_key": dedupe_key,
        "attempts": 0,
        "max_attempts": max_attempts,
        "scheduled_for": scheduled_for,
        "status": JobStatus.QUEUED.value,
    }

    # `.returning()` last: on_conflict_do_nothing is only available on the
    # dialect Insert, and returning() narrows the type away from it.
    stmt = pg_insert(Job).values(**values)

    if dedupe_key is not None:
        # ON CONFLICT DO NOTHING rather than catching IntegrityError.
        #
        # The exception route needs a SAVEPOINT, because a failed flush leaves
        # the whole session needing a rollback and would take the caller's
        # unrelated writes with it. Postgres resolves the conflict inside the
        # statement, so there is no failed flush to recover from at all.
        #
        # `index_where` must match the partial index predicate exactly, or
        # Postgres cannot infer which index to check.
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text("status IN ('queued', 'running') AND dedupe_key IS NOT NULL"),
        )

    job_id = db.execute(stmt.returning(Job.id)).scalar_one_or_none()

    if job_id is None:
        # An identical job is already queued or running. Not an error — the
        # outcome the caller wanted is already on its way.
        return None

    return db.get(Job, job_id)


def claim_next(db: Session, kinds: list[str] | None = None) -> Job | None:
    """
    Take the oldest eligible job, atomically.

    Args:
        db: Session. **The caller must commit promptly.** Until it does, the
            row lock is held — which is correct (no other worker can take it)
            but must not last the whole job, or Postgres carries an open
            transaction for minutes.
        kinds: Restrict to these kinds, for a worker dedicated to one queue.

    Returns:
        A job now marked ``running``, or None if there is nothing to do.

    Notes:
        ``FOR UPDATE SKIP LOCKED`` is the whole design. A second worker racing
        for the same row skips past it instead of blocking or duplicating, so
        workers need no coordination with each other at all.
    """
    now = datetime.now(UTC)

    candidate = (
        select(Job.id)
        .where(Job.status == JobStatus.QUEUED.value, Job.scheduled_for <= now)
        .order_by(Job.scheduled_for, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if kinds:
        candidate = candidate.where(Job.kind.in_(kinds))

    claimed = db.execute(
        update(Job)
        .where(Job.id == candidate.scalar_subquery())
        .values(
            status=JobStatus.RUNNING.value,
            started_at=now,
            attempts=Job.attempts + 1,
        )
        .returning(Job.id)
    ).scalar_one_or_none()

    if claimed is None:
        return None
    return db.get(Job, claimed)


def complete(db: Session, job: Job, result: dict[str, Any] | None = None) -> None:
    """Mark a job done. The caller commits."""
    job.status = JobStatus.SUCCEEDED.value
    job.result = result
    job.error = None
    job.finished_at = datetime.now(UTC)
    db.flush()


def fail(db: Session, job: Job, error: str, *, retry_in: timedelta | None = None) -> None:
    """
    Mark a job failed, or send it back for another attempt.

    Args:
        db: Session. The caller commits.
        job: The job that failed.
        error: A message safe to show whoever triggered the work.
        retry_in: How long to wait before retrying. Ignored once attempts have
            reached ``max_attempts``.

    Notes:
        Retrying is opt-in per job, not automatic per failure. A queue that
        retries a paid call by default turns one provider outage into a bill.
    """
    job.error = error

    if job.can_retry:
        job.status = JobStatus.QUEUED.value
        job.started_at = None
        # `is not None`, NOT `or`: timedelta(0) is FALSY, so `retry_in or
        # default` silently turns "retry immediately" into "retry in a minute".
        delay = retry_in if retry_in is not None else timedelta(minutes=1)
        job.scheduled_for = datetime.now(UTC) + delay
    else:
        job.status = JobStatus.FAILED.value
        job.finished_at = datetime.now(UTC)

    db.flush()


def reclaim_stalled(db: Session, *, stale_after: timedelta = STALE_AFTER) -> int:
    """
    Fail jobs whose worker died mid-run.

    Args:
        db: Session. The caller commits.
        stale_after: How long ``running`` is allowed to last.

    Returns:
        How many were reclaimed.

    Notes:
        **Failed, not requeued.** A job that died halfway may already have
        spent money and half-written its results; running it again could double
        the bill and duplicate the work. Failing it loudly puts the decision
        with a human, which for anything billable is the right place for it.

        Without this a crashed worker leaves rows in ``running`` forever and
        the dedupe key blocks the seller from ever retrying.
    """
    cutoff = datetime.now(UTC) - stale_after

    reclaimed = db.execute(
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING.value,
            Job.started_at.is_not(None),
            Job.started_at < cutoff,
        )
        .values(
            status=JobStatus.FAILED.value,
            error=("This job stopped unexpectedly and was not completed. It is safe to try again."),
            finished_at=datetime.now(UTC),
        )
        .returning(Job.id)
    ).all()

    db.flush()
    return len(reclaimed)


def get_job(db: Session, job_id: int, seller_id: int | None = None) -> Job | None:
    """
    Load a job, optionally scoped to its owner.

    Args:
        db: Session.
        job_id: From a URL, and therefore attacker-controlled.
        seller_id: When given, only that seller's jobs are visible.

    Returns:
        The job, or None — which the caller renders as a 404.

    Notes:
        The scope matters for the same reason it does on claims: job ids are
        sequential integers, and a job's ``result`` and ``error`` can name
        another seller's handles and post counts.
    """
    stmt = select(Job).where(Job.id == job_id)
    if seller_id is not None:
        stmt = stmt.where(Job.seller_id == seller_id)
    return db.scalar(stmt)
