"""
Database engine, session factory, and the declarative base.

    settings.database_url ──> engine ──> SessionLocal ──> get_db() ──> routes

WHY the pool options matter: Railway (like every cloud Postgres) silently drops
connections that have been idle for a while. Without ``pool_pre_ping`` the app
hands a request a dead connection and the user sees a 500 that vanishes on
retry — the worst kind of bug to chase. ``pool_recycle`` retires connections
before the provider does it for us.

RULE: import ``get_db`` for request-scoped sessions. Never build an engine
elsewhere; one engine per process is the whole point of a connection pool.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base every model inherits from.

    Alembic autogenerate compares the database against ``Base.metadata``, so a
    model that is never imported is invisible to migrations. ``app/models/__init__.py``
    imports them all for exactly this reason.
    """


engine = create_engine(
    settings.database_url_str,
    # Verify a connection is alive before handing it out. Costs one tiny round
    # trip; saves random 500s against a cloud database.
    pool_pre_ping=True,
    # Recycle connections after 5 minutes, comfortably under typical provider
    # idle timeouts.
    pool_recycle=300,
    # Echo SQL in dev only. In prod this would bury the logs that matter.
    echo=settings.app_env == "dev",
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency yielding a request-scoped session.

    The session is always closed, including when the route raises — otherwise a
    failing endpoint leaks a pool connection per request and the app dies after
    a few dozen errors.

    Yields:
        A SQLAlchemy session bound to this request.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
