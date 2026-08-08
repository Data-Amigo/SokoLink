"""SQLAlchemy models — the database rails.

Constraints defined here (``stock >= 0``, unique video ids, publish-requires-price)
are enforced by Postgres itself, so no code path — including a future agent —
can bypass them.

IMPORTANT: every model module must be imported here. Alembic autogenerate
compares the database against ``Base.metadata``, and a model that is never
imported is invisible to it — producing a migration that silently drops nothing
and creates nothing.

P1 adds Seller and Product.
"""

from app.db import Base

__all__ = ["Base"]
