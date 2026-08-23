"""email optional: a whatsapp signup has no email

Revision ID: 4c33b1e4d69f
Revises: c358826e129d
Created: 2026-08-23 15:11:44.586110

REVIEW BEFORE APPLYING. Autogenerate is a first draft, not an answer — it
misses renames (it sees a drop plus an add, which loses data), and it cannot
know about constraints you meant to add. Read every line.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '4c33b1e4d69f'
down_revision: str | None = 'c358826e129d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Let an account exist without an email.

    WRITTEN BY HAND. Autogenerate detects the nullability change but NOT the
    CheckConstraint edits — it never compares CHECKs on an existing table. The
    two email constraints would keep rejecting NULL, so the column would be
    nullable in name only and every WhatsApp signup would fail on a rail nobody
    could see in the model.
    """
    op.alter_column("accounts", "email", existing_type=sa.String(length=255), nullable=True)

    # Both must now tolerate NULL. Dropped and recreated: Postgres has no
    # "alter constraint" for a CHECK expression.
    op.drop_constraint("ck_accounts_email_lowercase", "accounts", type_="check")
    op.create_check_constraint(
        "ck_accounts_email_lowercase", "accounts", "email IS NULL OR email = lower(email)"
    )

    op.drop_constraint("ck_accounts_email_shape", "accounts", type_="check")
    op.create_check_constraint(
        "ck_accounts_email_shape", "accounts", "email IS NULL OR position('@' in email) > 1"
    )

    # The replacement rail. Without it, dropping NOT NULL would allow an account
    # with neither an email nor a phone — unreachable by every lookup path we
    # have, creatable only by a bug, and invisible once created.
    op.create_check_constraint(
        "ck_accounts_has_an_identity", "accounts", "email IS NOT NULL OR phone IS NOT NULL"
    )


def downgrade() -> None:
    """
    Restore the mandatory email.

    Refuses rather than destroys if any account has no email — those are real
    WhatsApp sellers, and inventing an address for them to satisfy a rollback
    would put unusable data in a unique column.
    """
    op.drop_constraint("ck_accounts_has_an_identity", "accounts", type_="check")

    op.drop_constraint("ck_accounts_email_shape", "accounts", type_="check")
    op.create_check_constraint(
        "ck_accounts_email_shape", "accounts", "position('@' in email) > 1"
    )

    op.drop_constraint("ck_accounts_email_lowercase", "accounts", type_="check")
    op.create_check_constraint(
        "ck_accounts_email_lowercase", "accounts", "email = lower(email)"
    )

    op.alter_column("accounts", "email", existing_type=sa.String(length=255), nullable=False)
