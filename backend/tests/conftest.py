"""
Shared pytest fixtures.

    test ──> db session (in a transaction) ──> ROLLED BACK afterwards
                    │
                    └──> injected into the app via dependency override

WHY the rollback pattern: tests run against a REAL Postgres, because that is the
only way to prove a constraint actually fires — SQLite would silently accept
things Postgres rejects, and the DB-level rails are the point. Wrapping each
test in a transaction that is always rolled back means the database is left
byte-identical afterwards, so tests can run against a live database without
polluting it and without an expensive truncate between cases.

External services (Apify, Gemini, Meta, Daraja) are ALWAYS mocked. Tests never
hit a paid API.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import Base, get_db
from app.main import app


def _test_database_url() -> str | None:
    """
    Resolve the test database, refusing anything unsafe.

    Two rules, both learned the hard way:

    1. Tests need their OWN database. They create tables and write rows.
    2. If TEST_DATABASE_URL is not set, database-backed tests **skip** rather
       than silently falling back to the app database. A skipped test is a
       visible gap; a test that quietly rewrites production data is a disaster.

    Raises:
        RuntimeError: If TEST_DATABASE_URL equals DATABASE_URL. That is never
            intentional, so it fails loudly rather than skipping.
    """
    test_url = settings.test_database_url_str
    if test_url is None:
        return None

    if test_url == settings.database_url_str:
        raise RuntimeError(
            "TEST_DATABASE_URL is the same as DATABASE_URL. The test suite "
            "creates tables and writes rows — point it at a separate database."
        )
    return test_url


#: Applied to every database-backed fixture below.
requires_db = pytest.mark.skipif(
    _test_database_url() is None,
    reason="TEST_DATABASE_URL is not set — see .env.example",
)


@pytest.fixture(scope="session")
def engine() -> Generator[Engine, None, None]:
    """One engine for the whole test session, bound to the TEST database."""
    url = _test_database_url()
    if url is None:
        pytest.skip("TEST_DATABASE_URL is not set")

    eng = create_engine(url, pool_pre_ping=True)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def _schema(engine: Engine) -> Generator[None, None, None]:
    """
    Rebuild the test schema from the models, once per session.

    DROPS then creates. ``create_all`` alone only adds MISSING tables — it never
    alters an existing one, so the moment a column is added to a model the test
    database silently drifts and every insert fails with a baffling
    "column does not exist". That cost real debugging time on 2026-08-08.

    Dropping guarantees the schema always matches the models exactly. It is safe
    because this only ever runs against TEST_DATABASE_URL, which
    ``_test_database_url()`` above refuses to let equal the application
    database.

    Uses the models rather than migrations deliberately: this proves the models
    are internally consistent, and it is fast. That migrations apply cleanly to
    an empty database is a separate guarantee, proved by the ``alembic upgrade
    head`` step in CI.

    Deliberately NOT autouse — it must only fire for tests that asked for a
    database, so the suite still runs without one.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def connection(engine: Engine, _schema: None) -> Generator[Connection, None, None]:
    """
    A connection with an open transaction that is always rolled back.

    The ``is_active`` guard matters here specifically because many tests in this
    suite deliberately trigger IntegrityError to prove a constraint refuses.
    Postgres aborts the transaction on such an error, so by the time this
    fixture tears down there may be nothing left to roll back — and calling
    rollback() anyway raises a SAWarning that would otherwise appear on every
    rail test and train us to ignore warnings.

    Isolation is unaffected either way: the connection is closed regardless, and
    each test gets a fresh one.
    """
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        if trans.is_active:
            trans.rollback()
        conn.close()


@pytest.fixture
def db(connection: Connection) -> Generator[Session, None, None]:
    """A session bound to the rolled-back transaction."""
    factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db: Session) -> Generator[TestClient, None, None]:
    """
    A TestClient whose routes use the rolled-back session.

    The dependency override is cleared afterwards so one test cannot leak its
    session into the next.
    """

    def _override() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
