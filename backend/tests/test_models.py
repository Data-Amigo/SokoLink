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

from app.models import (
    IngestMethod,
    Platform,
    Product,
    ProductStatus,
    ScrapeJob,
    ScrapeStatus,
)
from tests.factories import make_account, make_product, make_seller


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


class TestPostIdUniqueness:
    """RAIL 3 — re-scraping a feed updates rather than duplicates."""

    def test_the_same_post_cannot_be_stored_twice(self, db: Session) -> None:
        seller = make_seller(db)
        make_product(db, seller, platform_post_id="7100000000000000042")
        with pytest.raises(IntegrityError):
            make_product(db, seller, platform_post_id="7100000000000000042")

    def test_the_same_id_on_two_platforms_is_legal(self, db: Session) -> None:
        """
        Uniqueness is per platform. Two platforms can legitimately mint the
        same numeric id, and a global unique would reject the second one.
        """
        seller = make_seller(db)
        make_product(db, seller, platform=Platform.TIKTOK.value, platform_post_id="12345")
        make_product(
            db,
            seller,
            platform=Platform.INSTAGRAM.value,
            platform_post_id="12345",
            title="Same id, different platform",
        )
        assert db.query(Product).filter_by(platform_post_id="12345").count() == 2

    def test_many_uploads_can_have_no_post_id(self, db: Session) -> None:
        """
        Postgres allows multiple NULLs in a unique index — which is what lets
        uploads coexist without needing a partial index.
        """
        seller = make_seller(db)
        for i in range(3):
            make_product(
                db,
                seller,
                title=f"Uploaded item {i}",
                platform=Platform.MANUAL.value,
                ingest_method=IngestMethod.UPLOAD.value,
                platform_post_id=None,
            )
        assert db.query(Product).filter_by(platform=Platform.MANUAL.value).count() == 3


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


class TestProvenanceRails:
    """Provenance decides what a re-sync may touch, so it must be trustworthy."""

    def test_an_unknown_platform_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="platform_valid"):
            make_product(db, seller, platform="myspace")

    def test_an_unknown_ingest_method_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="ingest_method_valid"):
            make_product(db, seller, ingest_method="telepathy")

    def test_an_upload_cannot_carry_a_post_id(self, db: Session) -> None:
        """
        Uploads have no platform post by definition. Allowing one would make
        them look re-syncable and put seller-entered data at risk.
        """
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="upload_has_no_post_id"):
            make_product(
                db,
                seller,
                platform=Platform.MANUAL.value,
                ingest_method=IngestMethod.UPLOAD.value,
                platform_post_id="7100000000000000099",
            )

    def test_manual_and_upload_must_agree(self, db: Session) -> None:
        """A "tiktok upload" is incoherent — the two dimensions must match."""
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="manual_iff_upload"):
            make_product(
                db,
                seller,
                platform=Platform.TIKTOK.value,
                ingest_method=IngestMethod.UPLOAD.value,
                platform_post_id=None,
            )

    def test_all_three_ingestion_paths_are_accepted(self, db: Session) -> None:
        seller = make_seller(db)
        make_product(db, seller, ingest_method=IngestMethod.PROFILE_SYNC.value)
        make_product(
            db,
            seller,
            ingest_method=IngestMethod.SINGLE_LINK.value,
            platform_post_id="7100000000000000002",
            title="From a link",
        )
        make_product(
            db,
            seller,
            platform=Platform.MANUAL.value,
            ingest_method=IngestMethod.UPLOAD.value,
            platform_post_id=None,
            title="Uploaded by hand",
        )
        assert db.query(Product).filter_by(seller_id=seller.id).count() == 3

    def test_only_profile_sync_products_are_sync_owned(self, db: Session) -> None:
        """
        The rail that protects hand-entered stock: a feed sync owns only what
        it created. A seller who adds a product, syncs, and watches it vanish
        does not come back.
        """
        seller = make_seller(db)
        synced = make_product(db, seller, ingest_method=IngestMethod.PROFILE_SYNC.value)
        pasted = make_product(
            db,
            seller,
            ingest_method=IngestMethod.SINGLE_LINK.value,
            platform_post_id="7100000000000000002",
        )
        uploaded = make_product(
            db,
            seller,
            platform=Platform.MANUAL.value,
            ingest_method=IngestMethod.UPLOAD.value,
            platform_post_id=None,
        )

        assert synced.is_sync_owned is True
        assert pasted.is_sync_owned is False
        assert uploaded.is_sync_owned is False


class TestSocialAccountRails:
    """Connections are per platform, and a handle belongs to one seller only."""

    def test_a_seller_can_connect_several_platforms(self, db: Session) -> None:
        seller = make_seller(db)
        make_account(db, seller, platform=Platform.TIKTOK.value)
        make_account(db, seller, platform=Platform.INSTAGRAM.value, handle="nairobi_thrift")

        assert sorted(seller.connected_platforms) == ["instagram", "tiktok"]
        assert seller.has_any_connection is True

    def test_the_same_platform_cannot_be_connected_twice(self, db: Session) -> None:
        """Two TikToks would make "sync my feed" ambiguous."""
        seller = make_seller(db)
        make_account(db, seller, platform=Platform.TIKTOK.value, handle="one")
        with pytest.raises(IntegrityError):
            make_account(db, seller, platform=Platform.TIKTOK.value, handle="two")

    def test_one_handle_cannot_belong_to_two_sellers(self, db: Session) -> None:
        """Otherwise two shops could publish the same account's products."""
        first = make_seller(db, slug="first")
        second = make_seller(db, slug="second")
        make_account(db, first, handle="contested")
        with pytest.raises(IntegrityError):
            make_account(db, second, handle="contested")

    def test_manual_is_not_a_connectable_platform(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError):
            make_account(db, seller, platform=Platform.MANUAL.value)

    def test_account_for_finds_the_right_platform(self, db: Session) -> None:
        seller = make_seller(db)
        make_account(db, seller, platform=Platform.TIKTOK.value)

        assert seller.account_for(Platform.TIKTOK) is not None
        assert seller.account_for(Platform.INSTAGRAM) is None

    def test_a_disconnected_account_is_not_counted(self, db: Session) -> None:
        """Kept rather than deleted, so imported products keep a coherent origin."""
        seller = make_seller(db)
        make_account(db, seller, is_active=False)

        assert seller.connected_platforms == []
        assert seller.account_for(Platform.TIKTOK) is None

    def test_a_seller_can_exist_with_no_connections(self, db: Session) -> None:
        """Manual uploads alone are a legitimate way to run a shop."""
        seller = make_seller(db)
        assert seller.has_any_connection is False


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
        make_seller(db, slug="thrift")
        with pytest.raises(IntegrityError):
            make_seller(db, slug="thrift")


class TestScrapeJobRails:
    def test_a_failed_job_must_record_why(self, db: Session) -> None:
        """A failed scrape with no reason is unactionable for whoever debugs it."""
        seller = make_seller(db)
        job = ScrapeJob(
            seller_id=seller.id,
            status=ScrapeStatus.FAILED.value,
            platform=Platform.TIKTOK.value,
            ingest_method=IngestMethod.PROFILE_SYNC.value,
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
            platform=Platform.TIKTOK.value,
            ingest_method=IngestMethod.PROFILE_SYNC.value,
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
            platform=Platform.TIKTOK.value,
            ingest_method=IngestMethod.PROFILE_SYNC.value,
            video_count=0,
        )
        db.add(job)
        db.flush()
        assert job.error is None


class TestWholesalePricing:
    """
    Found by spike 04, not by design: @zumamitumbabales sells mitumba bales —
    "3000 for 30 pairs". A buyer shown a bare "KES 3,000" would expect one pair.
    """

    def test_a_bulk_price_always_renders_its_units(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=3000, unit_quantity=30, unit_label="pairs")
        assert product.is_wholesale is True
        assert product.price_display == "KES 3,000 for 30 pairs"

    def test_a_single_item_price_renders_plainly(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=1500)
        assert product.is_wholesale is False
        assert product.price_display == "KES 1,500"

    def test_a_quantity_of_one_is_not_wholesale(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=1500, unit_quantity=1, unit_label="piece")
        assert product.is_wholesale is False
        assert product.price_display == "KES 1,500"

    def test_no_price_means_no_display(self, db: Session) -> None:
        seller = make_seller(db)
        assert make_product(db, seller, price_kes=None).price_display is None

    def test_a_zero_or_negative_lot_size_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="unit_quantity_positive"):
            make_product(db, seller, price_kes=3000, unit_quantity=0, unit_label="pairs")

    def test_a_quantity_without_a_label_is_refused(self, db: Session) -> None:
        """ "KES 3,000 for 30" of what? Either both are known or neither is."""
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="unit_quantity_and_label_together"):
            make_product(db, seller, price_kes=3000, unit_quantity=30, unit_label=None)

    def test_a_label_without_a_quantity_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(IntegrityError, match="unit_quantity_and_label_together"):
            make_product(db, seller, price_kes=3000, unit_quantity=None, unit_label="pairs")

    def test_price_evidence_preserves_what_the_model_heard(self, db: Session) -> None:
        """The audit trail behind an AI-read price, and how bulk pricing surfaced."""
        seller = make_seller(db)
        product = make_product(
            db,
            seller,
            price_kes=3000,
            unit_quantity=30,
            unit_label="pairs",
            price_source="video",
            price_evidence="@3000 30pairs",
        )
        assert product.price_evidence == "@3000 30pairs"
        assert product.price_is_ai_drafted is True


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
