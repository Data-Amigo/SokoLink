"""wa_messages.provider_message_id: fit a Meta wamid

Revision ID: 07830c44ce7e
Revises: f0e5f6c9ee7f
Created: 2026-08-30 08:50:30.518426

REVIEW BEFORE APPLYING. Autogenerate is a first draft, not an answer — it
misses renames (it sees a drop plus an add, which loses data), and it cannot
know about constraints you meant to add. Read every line.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '07830c44ce7e'
down_revision: str | None = 'f0e5f6c9ee7f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Sized for Twilio's 34-character SM… SIDs. Meta's wamids are base64 and run
    # to about 80, so every inbound Meta message failed its INSERT, returned a
    # 500, and was redelivered — the bot never recorded a message and never
    # replied. Widening is safe on a live table: Postgres does not rewrite it
    # when a varchar limit only grows.
    op.alter_column(
        "wa_messages",
        "provider_message_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Any Meta wamid already stored is longer than 64 and would be truncated,
    # which would silently break the dedupe key. Refusing beats corrupting.
    op.execute(
        "DELETE FROM wa_messages WHERE length(provider_message_id) > 64"
    )
    op.alter_column(
        "wa_messages",
        "provider_message_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
