"""
The background worker — a second process that drains the job queue.

    python -m app.worker          (from backend/)

    ┌──────────────────────────────────────────────┐
    │  loop:                                       │
    │    reclaim_stalled()      every RECLAIM_EVERY│
    │    job = claim_next()     atomic, SKIP LOCKED│
    │      none  ──▶ sleep POLL_INTERVAL           │
    │      some  ──▶ HANDLERS[job.kind](db, job)   │
    │                  ok    ──▶ complete()        │
    │                  raise ──▶ fail()            │
    └──────────────────────────────────────────────┘

WHY A SEPARATE PROCESS. The web process must answer requests in milliseconds.
A three-minute Apify scrape sharing that process would hold a worker thread for
the whole run; enough of them and the site stops responding for everyone. On
Railway this is a second service pointed at the same repo and the same database.

WHY POLLING RATHER THAN LISTEN/NOTIFY. Polling once a second is one trivial
indexed query per second, and it survives a dropped connection with no
resubscribe logic. NOTIFY is more elegant and would matter at a volume we are
nowhere near.

**A HANDLER MUST NEVER COMMIT.** This loop owns the transaction, so that a
handler which raises halfway leaves nothing half-written. It commits once, after
the handler returns.

ADDING A JOB KIND: write the handler, register it in HANDLERS. Nothing else.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Job
from app.services.jobs import claim_next, complete, fail, reclaim_stalled

logger = logging.getLogger("biashara.worker")

#: How long to wait when the queue is empty. Short enough that a seller
#: pressing Sync does not watch a spinner for nothing.
POLL_INTERVAL = 1.0

#: How often to look for jobs whose worker died. Cheap, so it need not be rare;
#: not free, so it need not be constant.
RECLAIM_EVERY = timedelta(minutes=5)

#: kind ──▶ handler. A handler takes (db, job) and returns a small JSON-able
#: dict for the UI, or None. It must not commit and must not swallow errors:
#: raising is how a job is marked failed.
HANDLERS: dict[str, Callable[[Session, Job], dict[str, Any] | None]] = {}


def register(kind: str) -> Callable[..., Any]:
    """
    Decorator registering a handler for a job kind.

    Args:
        kind: Must match the ``kind`` used when enqueueing.

    Returns:
        The handler, unchanged.

    Raises:
        ValueError: On a duplicate registration — two handlers for one kind
            means whichever imported last silently wins, which is the sort of
            bug that only shows up in production.
    """

    def decorator(
        fn: Callable[[Session, Job], dict[str, Any] | None],
    ) -> Callable[[Session, Job], dict[str, Any] | None]:
        if kind in HANDLERS:
            raise ValueError(f"A handler for job kind {kind!r} is already registered")
        HANDLERS[kind] = fn
        return fn

    return decorator


class Worker:
    """Drains the queue until asked to stop."""

    def __init__(self, kinds: list[str] | None = None) -> None:
        self.kinds = kinds
        self.running = True
        self._last_reclaim = datetime.now(UTC)

    def stop(self, signum: int, frame: FrameType | None) -> None:
        """
        Ask the loop to finish the job in hand and then exit.

        Deliberately not immediate. Killing a worker mid-job leaves a row in
        ``running`` that only the stale-reclaimer will clear, and for a paid
        call it may mean money spent with nothing recorded.
        """
        logger.info("Signal %s received — finishing current job, then stopping", signum)
        self.running = False

    def run_once(self, db: Session) -> bool:
        """
        Claim and run at most one job.

        Args:
            db: A session for this iteration.

        Returns:
            True if a job ran, False if the queue was empty — which is what
            tells the caller whether to sleep.
        """
        job = claim_next(db, kinds=self.kinds)
        if job is None:
            return False

        # Make the claim durable immediately. Until this commits, the row lock
        # is held — correct, but holding it for a three-minute scrape means an
        # open transaction for three minutes, which Postgres pays for in bloat.
        db.commit()

        handler = HANDLERS.get(job.kind)
        if handler is None:
            # Unregistered kind. Fail it loudly rather than leaving it queued
            # forever where nobody would look for it.
            logger.error("Job %s has unknown kind %r", job.id, job.kind)
            fail(db, job, f"No handler is registered for {job.kind!r}.")
            db.commit()
            return True

        logger.info("Job %s (%s) starting, attempt %s", job.id, job.kind, job.attempts)
        started = time.monotonic()

        try:
            result = handler(db, job)
        except Exception as exc:  # noqa: BLE001 — a failed job must not stop the worker
            db.rollback()
            # Re-fetched after the rollback: the instance is expired, and the
            # failure must be recorded on a clean transaction.
            fresh = db.get(Job, job.id)
            if fresh is not None:
                fail(db, fresh, f"{type(exc).__name__}: {exc}")
                db.commit()
            logger.exception("Job %s failed", job.id)
            return True

        complete(db, job, result)
        db.commit()
        logger.info("Job %s done in %.1fs", job.id, time.monotonic() - started)
        return True

    def maybe_reclaim(self, db: Session) -> None:
        """Periodically fail jobs whose worker died."""
        if datetime.now(UTC) - self._last_reclaim < RECLAIM_EVERY:
            return

        count = reclaim_stalled(db)
        db.commit()
        self._last_reclaim = datetime.now(UTC)
        if count:
            logger.warning("Reclaimed %s stalled job(s)", count)

    def run(self) -> None:
        """Loop until stopped."""
        logger.info("Worker started. Handlers: %s", sorted(HANDLERS) or "(none)")

        while self.running:
            db = SessionLocal()
            try:
                self.maybe_reclaim(db)
                did_work = self.run_once(db)
            except Exception:  # noqa: BLE001 — the loop must outlive any one error
                # A database blip must not take the worker down; systemd or
                # Railway restarting it would be a slower version of waiting.
                logger.exception("Worker iteration failed")
                did_work = False
            finally:
                db.close()

            if not did_work:
                time.sleep(POLL_INTERVAL)

        logger.info("Worker stopped")


def main() -> int:
    """Entry point for ``python -m app.worker``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Importing the handlers registers them. Done here rather than at module
    # scope so that importing this module — as the tests do — does not drag in
    # the whole service layer.
    from app import jobs_handlers  # noqa: F401

    worker = Worker()
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)

    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
