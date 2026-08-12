"""
Object factories for tests.

Every factory returns a VALID object and takes overrides, so a test changes only
the one field it is about. That keeps the interesting difference visible instead
of buried in ten lines of scaffolding.

    seller  = make_seller(db)
    account = make_account(db, seller, platform="instagram")
    product = make_product(db, seller, price_kes=3000)

Lives here rather than in a test module because test modules must not import
each other — the same file then resolves under two module names, and mypy is
right to reject that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    IngestMethod,
    Platform,
    Product,
    Seller,
    SocialAccount,
    VerificationMethod,
)


def make_seller(db: Session, **overrides: Any) -> Seller:
    """A valid seller with a WhatsApp number, so it could be published."""
    values: dict[str, Any] = {
        "slug": "nairobithrift",
        "display_name": "Nairobi Thrift",
        "whatsapp_number": "254712345678",
    }
    values.update(overrides)
    seller = Seller(**values)
    db.add(seller)
    db.flush()
    return seller


def make_account(db: Session, seller: Seller, **overrides: Any) -> SocialAccount:
    """
    A connected social account — TikTok unless overridden.

    NECESSARILY VERIFIED. There is no unverified kind: ``verified_at`` and
    ``verification_method`` are NOT NULL, so an unproven connection cannot be
    stored at all. An unproven attempt is an ``AccountClaim``, which is a
    different table and grants nothing.

    That is why this factory cannot offer an "unverified account" option — the
    thing does not exist. Tests that need one build a claim instead, via
    ``services.verification.start_claim``.
    """
    values: dict[str, Any] = {
        "seller_id": seller.id,
        "platform": Platform.TIKTOK.value,
        "handle": "nairobithrift",
        "display_name": "Nairobi Thrift",
        "verified_at": datetime.now(UTC),
        "verification_method": VerificationMethod.BIO_CODE.value,
    }
    values.update(overrides)
    account = SocialAccount(**values)
    db.add(account)
    db.flush()
    return account


def make_product(db: Session, seller: Seller, **overrides: Any) -> Product:
    """A valid DRAFT product from a profile sync, with no price yet."""
    values: dict[str, Any] = {
        "seller_id": seller.id,
        "title": "Cargo Pants",
        "platform": Platform.TIKTOK.value,
        "ingest_method": IngestMethod.PROFILE_SYNC.value,
        "platform_post_id": "7100000000000000001",
    }
    values.update(overrides)
    product = Product(**values)
    db.add(product)
    db.flush()
    return product
