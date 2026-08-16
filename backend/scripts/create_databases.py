"""
Create Biashara Mall's application and test databases on an existing Postgres server.

    python scripts/create_databases.py

WHY this exists rather than "just provision another Railway service": a Postgres
SERVER hosts many DATABASES. Two databases on one server give full isolation —
Postgres databases cannot see each other's tables — at zero extra cost and with
no new infrastructure to run. Docker is unnecessary for this.

    server (Railway)
      ├── railway         ← pre-existing POC data, NEVER touched by this script
      ├── biashara        ← the application database
      └── biashara_test   ← the test database (tests create and drop tables here)

It connects using DATABASE_URL from .env, which may point at any database on the
target server — it only needs a connection, since CREATE DATABASE is a
server-level operation.

Idempotent: an existing database is left exactly as it is, never recreated.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running this file directly puts scripts/ on sys.path, not backend/, so `app`
# is not importable. Add the backend root before importing from it. Must happen
# before the app import below, hence the noqa.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.config import settings  # noqa: E402

#: Databases this project needs. Anything already present is left alone.
DATABASES = ("biashara", "biashara_test")


def main() -> int:
    # psycopg wants a bare postgresql:// DSN; the +psycopg suffix is SQLAlchemy's.
    dsn = settings.database_url_str.replace("postgresql+psycopg://", "postgresql://")

    # autocommit is required: CREATE DATABASE cannot run inside a transaction.
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
        if not row or not row[0]:
            print(
                "This role cannot create databases. Ask your provider for a role "
                "with CREATEDB, or create them by hand.",
                file=sys.stderr,
            )
            return 1

        for name in DATABASES:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
            if cur.fetchone():
                print(f"  {name:<16} already exists — left alone")
                continue

            # The name is a literal from DATABASES above, never user input, so
            # the f-string is safe here. CREATE DATABASE cannot take a bound
            # parameter for the identifier.
            cur.execute(f'CREATE DATABASE "{name}"')
            print(f"  {name:<16} created")

        cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
        print("\nDatabases on this server:", ", ".join(r[0] for r in cur.fetchall()))

    print(
        "\nNext: point .env at them —\n"
        "  DATABASE_URL      -> .../biashara\n"
        "  TEST_DATABASE_URL -> .../biashara_test"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
