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

from typing import Any

from sqlalchemy.orm import Session

from app.models import IngestMethod, Platform, Product, Seller, SocialAccount


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
    A connected social account — TikTok, and deliberately UNVERIFIED.

    Unverified is the honest default: verification is opt-in work a seller has
    to do, and a factory that quietly pre-verified would hide the very rail
    most of these tests exist to check.
    """
    values: dict[str, Any] = {
        "seller_id": seller.id,
        "platform": Platform.TIKTOK.value,
        "handle": "nairobithrift",
        "display_name": "Nairobi Thrift",
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
