"""
Alembic environment — wires migrations to the application's own config.

    app.config.settings ──> database URL ──> migration engine
    app.models.Base.metadata ──> autogenerate target

WHY it reads settings rather than alembic.ini: the URL is a secret, alembic.ini
is committed, and two sources of one value drift. This way the app, the tests
and the migrations always agree about which database they mean — and switching
environments is a .env change, not an edit to a tracked file.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import settings
from app.models import Base

# NOTE ON THAT LAST IMPORT — it looks unused, and it is load-bearing.
# Importing the models package registers every model on Base.metadata. Without
# it, autogenerate sees an empty schema and cheerfully writes a migration that
# drops every table. Do not "tidy" it away.

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the real URL at runtime. alembic.ini holds a blank value on purpose.
config.set_main_option("sqlalchemy.url", settings.database_url_str)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — used to review a migration."""
    context.configure(
        url=settings.database_url_str,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detect column type changes, not just added/dropped columns.
            # Without this, widening a column produces an empty migration.
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
