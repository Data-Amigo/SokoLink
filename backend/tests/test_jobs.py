"""
Tests for the job queue and the worker loop.

Four things are protected here, and three of them are about money:

  **A job is claimed exactly once.** Two workers polling the same table both
  see the same queued row. If both claim it, a paid scrape is bought twice.
  ``SELECT … FOR UPDATE SKIP LOCKED`` is what prevents that, and it is worth an
  explicit test against real Postgres — SQLite would happily pretend it works.

  **Duplicate work collapses.** A seller pressing Sync twice must not buy two
  scrapes while the first is still pending.

  **Retries are opt-in.** A queue that retries a paid call by default turns one
  provider outage into a bill.

  **A dead worker does not wedge the queue.** A row stuck in ``running`` holds
  its dedupe key forever, which would block the seller from ever trying again.

Nothing here touches a paid API. The handlers are local functions.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Job, JobStatus, Seller
from app.services.jobs import (
    claim_next,
    complete,
    enqueue,
    fail,
    get_job,
    reclaim_stalled,
)
from tests.factories import make_seller


@pytest.fixture
def db(engine: Engine, _schema: None) -> Generator[Session, None, None]:
    """
    A session that REALLY COMMITS, against the test database.

    Every other module in this suite uses the rollback-based `db` fixture from
    conftest, and deliberately so — it is fast and leaves the database
    byte-identical.

    A queue cannot be tested that way. Its correctness *is* its transaction
    behaviour: the worker commits a claim so that other workers can see it, and
    rolls back a failed handler so nothing is half-written. Run that through a
    fixture where commit is a savepoint release and rollback unwinds the test's
    own setup, and you are testing the fixture, not the queue.

    So this one commits for real and cleans up after itself. Overriding `db` by
    name rather than inventing a second one keeps every test in this file on
    the honest session without having to remember which to ask for.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        # Jobs first: sellers cascade to jobs, but jobs also exist without a
        # seller, so deleting sellers alone would leave orphans behind for the
        # next test to trip over.
        session.execute(delete(Job))
        session.execute(delete(Seller))
        session.commit()
        session.close()


class TestEnqueue:
    def test_a_job_starts_queued(self, db: Session) -> None:
        job = enqueue(db, "sync_posts", payload={"social_account_id": 1})

        assert job is not None
        assert job.status == JobStatus.QUEUED.value
        assert job.attempts == 0
        assert job.payload == {"social_account_id": 1}

    def test_retries_are_off_unless_asked_for(self, db: Session) -> None:
        """Nearly every job here spends money. Retrying must be a decision."""
        job = enqueue(db, "sync_posts")

        assert job is not None
        assert job.max_attempts == 1

    def test_a_delayed_job_is_not_yet_claimable(self, db: Session) -> None:
        enqueue(db, "sync_posts", delay=timedelta(hours=1))

        assert claim_next(db) is None

    def test_the_job_belongs_to_a_seller(self, db: Session) -> None:
        """Attribution the spend ledger will need in step 6."""
        seller = make_seller(db)

        job = enqueue(db, "sync_posts", seller_id=seller.id)

        assert job is not None
        assert job.seller_id == seller.id


class TestDeduplication:
    def test_the_same_work_queued_twice_collapses(self, db: Session) -> None:
        """A seller pressing Sync twice must not buy two scrapes."""
        first = enqueue(db, "sync_posts", dedupe_key="sync:7")
        second = enqueue(db, "sync_posts", dedupe_key="sync:7")

        assert first is not None
        assert second is None, "the duplicate must collapse, not raise"
        assert db.scalar(select(Job.id).where(Job.dedupe_key == "sync:7")) == first.id

    def test_losing_the_dedupe_race_does_not_poison_the_transaction(self, db: Session) -> None:
        """
        Postgres aborts the whole transaction on a constraint violation, and
        the caller may have unrelated work in flight. The savepoint is what
        keeps a collapsed duplicate from taking a route's other writes with it.
        """
        enqueue(db, "sync_posts", dedupe_key="sync:7")
        enqueue(db, "sync_posts", dedupe_key="sync:7")

        seller = make_seller(db)
        assert seller.id is not None, "the session must still be usable"

    def test_the_key_is_reusable_once_the_job_finishes(self, db: Session) -> None:
        """
        The index is PARTIAL on purpose. A seller may sync again tomorrow — a
        plain unique index would let them sync once, ever.
        """
        first = enqueue(db, "sync_posts", dedupe_key="sync:7")
        assert first is not None
        complete(db, first, {"ok": True})

        second = enqueue(db, "sync_posts", dedupe_key="sync:7")

        assert second is not None
        assert second.id != first.id

    def test_jobs_without_a_key_never_collapse(self, db: Session) -> None:
        assert enqueue(db, "cleanup") is not None
        assert enqueue(db, "cleanup") is not None


class TestClaiming:
    def test_claiming_marks_the_job_running(self, db: Session) -> None:
        enqueue(db, "sync_posts")

        job = claim_next(db)

        assert job is not None
        assert job.status == JobStatus.RUNNING.value
        assert job.attempts == 1
        assert job.started_at is not None

    def test_a_claimed_job_is_not_handed_out_again(self, db: Session) -> None:
        """
        THE test. If this fails, two workers run the same paid job.

        Real Postgres, real SKIP LOCKED — the reason this suite refuses to run
        on SQLite.
        """
        enqueue(db, "sync_posts")

        first = claim_next(db)
        second = claim_next(db)

        assert first is not None
        assert second is None

    def test_the_oldest_eligible_job_goes_first(self, db: Session) -> None:
        older = enqueue(db, "sync_posts", payload={"n": 1})
        enqueue(db, "sync_posts", payload={"n": 2})
        assert older is not None

        claimed = claim_next(db)

        assert claimed is not None
        assert claimed.id == older.id

    def test_a_worker_can_restrict_itself_to_certain_kinds(self, db: Session) -> None:
        enqueue(db, "render_video")

        assert claim_next(db, kinds=["sync_posts"]) is None
        assert claim_next(db, kinds=["render_video"]) is not None

    def test_an_empty_queue_returns_nothing(self, db: Session) -> None:
        assert claim_next(db) is None


class TestOutcomes:
    def test_completing_records_the_result(self, db: Session) -> None:
        enqueue(db, "sync_posts")
        job = claim_next(db)
        assert job is not None

        complete(db, job, {"posts": 10})

        assert job.status == JobStatus.SUCCEEDED.value
        assert job.result == {"posts": 10}
        assert job.finished_at is not None

    def test_failing_without_retries_is_final(self, db: Session) -> None:
        enqueue(db, "sync_posts")
        job = claim_next(db)
        assert job is not None

        fail(db, job, "Apify is down")

        assert job.status == JobStatus.FAILED.value
        assert job.error == "Apify is down"
        assert claim_next(db) is None, "a failed job must not be re-claimed"

    def test_failing_with_retries_left_requeues_it(self, db: Session) -> None:
        enqueue(db, "cleanup", max_attempts=3)
        job = claim_next(db)
        assert job is not None

        fail(db, job, "transient", retry_in=timedelta(seconds=0))

        assert job.status == JobStatus.QUEUED.value
        assert job.attempts == 1

    def test_retries_are_exhausted_eventually(self, db: Session) -> None:
        enqueue(db, "cleanup", max_attempts=2)

        last: Job | None = None
        for _ in range(2):
            last = claim_next(db)
            assert last is not None
            fail(db, last, "still broken", retry_in=timedelta(seconds=0))

        assert last is not None
        assert last.status == JobStatus.FAILED.value
        assert last.attempts == 2


class TestStalledJobs:
    def test_a_job_whose_worker_died_is_failed(self, db: Session) -> None:
        """
        Otherwise the row sits in `running` forever holding its dedupe key,
        and the seller can never try again.
        """
        enqueue(db, "sync_posts", dedupe_key="sync:7")
        job = claim_next(db)
        assert job is not None
        job.started_at = datetime.now(UTC) - timedelta(hours=2)
        db.flush()

        assert reclaim_stalled(db) == 1
        db.refresh(job)
        assert job.status == JobStatus.FAILED.value
        assert "try again" in (job.error or "")

    def test_reclaiming_frees_the_dedupe_key(self, db: Session) -> None:
        enqueue(db, "sync_posts", dedupe_key="sync:7")
        job = claim_next(db)
        assert job is not None
        job.started_at = datetime.now(UTC) - timedelta(hours=2)
        db.flush()
        reclaim_stalled(db)

        assert enqueue(db, "sync_posts", dedupe_key="sync:7") is not None

    def test_a_merely_slow_job_is_left_alone(self, db: Session) -> None:
        """
        A cold Apify actor can take minutes. Reclaiming it would run a paid
        call twice — exactly what the queue exists to prevent.
        """
        enqueue(db, "sync_posts")
        claim_next(db)

        assert reclaim_stalled(db) == 0

    def test_a_stalled_job_is_not_requeued(self, db: Session) -> None:
        """
        It may already have spent money and half-written its results. Failing
        it loudly puts the decision with a human.
        """
        enqueue(db, "sync_posts")
        job = claim_next(db)
        assert job is not None
        job.started_at = datetime.now(UTC) - timedelta(hours=2)
        db.flush()
        reclaim_stalled(db)

        assert claim_next(db) is None


class TestOwnership:
    def test_a_job_can_be_scoped_to_its_seller(self, db: Session) -> None:
        """Job ids are sequential, and results name handles and post counts."""
        mine = make_seller(db)
        theirs = make_seller(db, slug="stranger", display_name="Stranger")
        job = enqueue(db, "sync_posts", seller_id=theirs.id)
        assert job is not None

        assert get_job(db, job.id, seller_id=mine.id) is None
        assert get_job(db, job.id, seller_id=theirs.id) is not None


class TestWorkerLoop:
    def test_a_handler_runs_and_the_job_succeeds(self, db: Session) -> None:
        from app.worker import HANDLERS, Worker

        HANDLERS["test_ok"] = lambda db, job: {"did": "work"}
        try:
            enqueue(db, "test_ok")
            db.commit()

            assert Worker().run_once(db) is True

            job = db.scalars(select(Job)).one()
            assert job.status == JobStatus.SUCCEEDED.value
            assert job.result == {"did": "work"}
        finally:
            HANDLERS.pop("test_ok", None)

    def test_a_raising_handler_fails_the_job_without_stopping_the_worker(self, db: Session) -> None:
        """One bad job must not take the worker down with it."""
        from app.worker import HANDLERS, Worker

        def boom(db: Session, job: Job) -> None:
            raise RuntimeError("the actor exploded")

        HANDLERS["test_boom"] = boom
        try:
            enqueue(db, "test_boom")
            db.commit()

            assert Worker().run_once(db) is True

            job = db.scalars(select(Job)).one()
            assert job.status == JobStatus.FAILED.value
            assert "the actor exploded" in (job.error or "")
        finally:
            HANDLERS.pop("test_boom", None)

    def test_an_unknown_kind_fails_loudly_rather_than_sitting_forever(self, db: Session) -> None:
        from app.worker import Worker

        enqueue(db, "nobody_handles_this")
        db.commit()

        assert Worker().run_once(db) is True

        job = db.scalars(select(Job)).one()
        assert job.status == JobStatus.FAILED.value
        assert "No handler" in (job.error or "")

    def test_an_empty_queue_reports_no_work(self, db: Session) -> None:
        from app.worker import Worker

        assert Worker().run_once(db) is False

    def test_registering_a_kind_twice_is_refused(self) -> None:
        """Two handlers for one kind means whichever imported last silently wins."""
        from app.worker import HANDLERS, register

        HANDLERS["test_dup"] = lambda db, job: None
        try:
            with pytest.raises(ValueError, match="already registered"):
                register("test_dup")(lambda db, job: None)
        finally:
            HANDLERS.pop("test_dup", None)
