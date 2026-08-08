"""
Tests for the database rails.

These are the most valuable tests in the codebase right now. Every one asserts
that Postgres **refuses** something — because a constraint nobody has watched
fail is only a claim. If a future refactor, migration, or agent drops one of
these rails, a test breaks here rather than a buyer being quoted a wrong price.

They run against real Postgres for exactly that reason: SQLite silently accepts
several of the things asserted below.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Product, ProductSource, ProductStatus, ScrapeJob, ScrapeStatus, Seller


def make_seller(db: Session, **overrides: object) -> Seller:
    """A valid seller, with fields overridable per test."""
    values: dict[str, object] = {
        "tiktok_handle": "nairobithrift",
        "slug": "nairobithrift",
        "display_name": "Nairobi Thrift",
        "whatsapp_number": "254712345678",
    }
    values.update(overrides)
    seller = Seller(**values)
    db.add(seller)
    db.flush()
    return seller


def make_product(db: Session, seller: Seller, **overrides: object) -> Product:
    """A valid draft product, with fields overridable per test."""
    values: dict[str, object] = {
        "seller_id": seller.id,
        "title": "Cargo Pants",
        "source": ProductSource.TIKTOK_PROFILE.value,
        "tiktok_video_id": "7100000000000000001",
    }
    values.update(overrides)
    product = Product(**values)
    db.add(product)
    db.flush()
    return product


class TestPublishRequiresPrice:
    """RAIL 1 — the AI can never push an unpriced item live."""

    def test_publishing_without_a_price_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="published_requires_price"):
            make_product(db, seller, status=ProductStatus.PUBLISHED.value, price_kes=None)

    def test_publishing_with_a_price_is_allowed(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, status=ProductStatus.PUBLISHED.value, price_kes=1500)
        assert product.is_buyable is True

    def test_a_draft_may_have_no_price(self, db: Session) -> None:
        """Drafts are where unpriced items are supposed to live."""
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=None)
        assert product.status == ProductStatus.DRAFT.value
        assert product.is_buyable is False


class TestStockRail:
    """RAIL 2 — overselling is impossible at the storage layer."""

    def test_negative_stock_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="stock_non_negative"):
            make_product(db, seller, stock=-1)

    def test_zero_stock_is_allowed_but_not_buyable(self, db: Session) -> None:
        """Sold out is a legitimate state, not an error."""
        seller = make_seller(db)
        product = make_product(
            db, seller, stock=0, status=ProductStatus.PUBLISHED.value, price_kes=1500
        )
        assert product.is_buyable is False


class TestVideoIdUniqueness:
    """RAIL 3 — re-scraping a feed updates rather than duplicates."""

    def test_the_same_video_cannot_be_stored_twice(self, db: Session) -> None:
        seller = make_seller(db)
        make_product(db, seller, tiktok_video_id="7100000000000000042")
        with pytest.raises(IntegrityError):
            make_product(db, seller, tiktok_video_id="7100000000000000042")

    def test_many_manual_products_can_have_no_video_id(self, db: Session) -> None:
        """
        Postgres allows multiple NULLs in a unique index — which is what lets
        manual uploads coexist without a partial index.
        """
        seller = make_seller(db)
        for i in range(3):
            make_product(
                db,
                seller,
                title=f"Uploaded item {i}",
                source=ProductSource.MANUAL.value,
                tiktok_video_id=None,
            )
        assert db.query(Product).filter_by(source=ProductSource.MANUAL.value).count() == 3


class TestPriceSanity:
    """A wrong price is worse than a missing one, so implausible values are refused."""

    def test_zero_price_is_refused(self, db: Session) -> None:
        """KSh 0 is always a parse failure, never a giveaway."""
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="price_positive"):
            make_product(db, seller, price_kes=0)

    def test_negative_price_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="price_positive"):
            make_product(db, seller, price_kes=-500)

    def test_an_implausibly_large_price_is_refused(self, db: Session) -> None:
        """The phone-number misparse: a model reading 0712345678 as a price."""
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="price_plausible"):
            make_product(db, seller, price_kes=712345678)

    def test_confidence_outside_zero_to_one_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="confidence_range"):
            make_product(db, seller, parse_confidence=1.5)


class TestSourceRails:
    """Provenance decides what a re-sync may touch, so it must be trustworthy."""

    def test_an_unknown_source_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="source_valid"):
            make_product(db, seller, source="instagram")

    def test_a_manual_product_cannot_carry_a_video_id(self, db: Session) -> None:
        """
        Manual products have no TikTok video by definition. Allowing one would
        make them look re-syncable and put seller-entered data at risk.
        """
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="manual_has_no_video_id"):
            make_product(
                db,
                seller,
                source=ProductSource.MANUAL.value,
                tiktok_video_id="7100000000000000099",
            )

    def test_all_three_ingestion_paths_are_accepted(self, db: Session) -> None:
        seller = make_seller(db)
        make_product(
            db,
            seller,
            source=ProductSource.TIKTOK_PROFILE.value,
            tiktok_video_id="7100000000000000001",
        )
        make_product(
            db,
            seller,
            source=ProductSource.TIKTOK_LINK.value,
            tiktok_video_id="7100000000000000002",
            title="From a link",
        )
        make_product(
            db,
            seller,
            source=ProductSource.MANUAL.value,
            tiktok_video_id=None,
            title="Uploaded by hand",
        )
        assert db.query(Product).filter_by(seller_id=seller.id).count() == 3


class TestSellerRails:
    def test_a_published_shop_needs_a_whatsapp_number(self, db: Session) -> None:
        """A published shop with no contact ends every buyer journey in silence."""
        with pytest.raises(IntegrityError, match="published_needs_whatsapp"):
            make_seller(db, is_published=True, whatsapp_number=None)

    def test_a_malformed_slug_is_refused(self, db: Session) -> None:
        """Slugs are URLs buyers type by hand — shape is enforced in the DB."""
        with pytest.raises(IntegrityError, match="slug_format"):
            make_seller(db, slug="Nairobi Thrift!")

    def test_duplicate_slugs_are_refused(self, db: Session) -> None:
        make_seller(db, slug="thrift", tiktok_handle="one")
        with pytest.raises(IntegrityError):
            make_seller(db, slug="thrift", tiktok_handle="two")

    def test_a_seller_can_exist_without_a_tiktok_handle(self, db: Session) -> None:
        """Manual-upload-only sellers are legitimate — TikTok is optional."""
        seller = make_seller(db, tiktok_handle=None, slug="handmade-crafts")
        assert seller.tiktok_handle is None


class TestScrapeJobRails:
    def test_a_failed_job_must_record_why(self, db: Session) -> None:
        """A failed scrape with no reason is unactionable for whoever debugs it."""
        seller = make_seller(db)
        job = ScrapeJob(
            seller_id=seller.id,
            status=ScrapeStatus.FAILED.value,
            source=ProductSource.TIKTOK_PROFILE.value,
            error=None,
        )
        db.add(job)
        with pytest.raises(IntegrityError, match="failed_needs_error"):
            db.flush()

    def test_a_failed_job_with_a_reason_is_accepted(self, db: Session) -> None:
        seller = make_seller(db)
        job = ScrapeJob(
            seller_id=seller.id,
            status=ScrapeStatus.FAILED.value,
            source=ProductSource.TIKTOK_PROFILE.value,
            error="Apify actor timed out after 300s",
        )
        db.add(job)
        db.flush()
        assert job.is_terminal is True

    def test_a_succeeded_job_with_no_videos_is_valid(self, db: Session) -> None:
        """A private or empty profile is not an error — and must be distinguishable."""
        seller = make_seller(db)
        job = ScrapeJob(
            seller_id=seller.id,
            status=ScrapeStatus.SUCCEEDED.value,
            source=ProductSource.TIKTOK_PROFILE.value,
            video_count=0,
        )
        db.add(job)
        db.flush()
        assert job.error is None


class TestDerivedProperties:
    def test_needs_review_is_true_until_a_human_confirms(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=1500)
        assert product.needs_review is True

    def test_an_ai_drafted_price_is_flagged_as_such(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=600, price_source="cover_image")
        assert product.price_is_ai_drafted is True

    def test_a_seller_entered_price_is_not_flagged_as_ai(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=600, price_source="seller")
        assert product.price_is_ai_drafted is False
