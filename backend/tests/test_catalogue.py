"""
The publish gate.

This is the rule that was written into three documents, enforced by two database
constraints, and **implemented nowhere** until now. Nothing in the application
ever set a product to PUBLISHED; the only two lines that did lived in a seed
script. A real seller could sign up, connect M-Pesa, add stock and reach a 404.

So these tests do two jobs: they check the gate works, and they exist so that
the gate cannot quietly disappear again.

The two that matter most:

    publishing without a price is refused, with a message naming the item
    one seller can never publish, unpublish or reach another's stock
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import IngestMethod, Platform, PriceSource, Product, ProductStatus, Seller
from app.services.catalogue import (
    PublishError,
    catalogue_summary,
    create_product,
    get_own_product,
    list_products,
    publish_product,
    publish_shop,
    unpublish_product,
    unpublish_shop,
)
from tests.factories import make_product, make_seller


def draft(db: Session, seller: Seller, **overrides: Any) -> Product:
    """A draft product with a price — publishable unless a test says otherwise."""
    values: dict[str, Any] = {"price_kes": 1500, "stock": 3}
    values.update(overrides)
    return make_product(db, seller, **values)


class TestPublishingAProduct:
    def test_publishing_makes_it_buyable(self, db: Session) -> None:
        seller = make_seller(db)
        product = draft(db, seller)

        publish_product(db, seller, product.id)

        assert product.status == ProductStatus.PUBLISHED.value

    def test_publishing_without_a_price_is_refused(self, db: Session) -> None:
        """
        THE RAIL. An unpriced product on a storefront is a buyer being asked to
        pay an unknown number. Postgres refuses it too — this exists so the
        seller gets a sentence instead of an IntegrityError.
        """
        seller = make_seller(db)
        product = draft(db, seller, price_kes=None, title="Cargo Pants")

        with pytest.raises(PublishError, match="needs a price"):
            publish_product(db, seller, product.id)

        assert product.status == ProductStatus.DRAFT.value

    def test_the_refusal_names_the_item(self, db: Session) -> None:
        """A seller with thirty drafts needs to know WHICH one."""
        seller = make_seller(db)
        product = draft(db, seller, price_kes=None, title="Leather Bag")

        with pytest.raises(PublishError, match="Leather Bag"):
            publish_product(db, seller, product.id)

    def test_publishing_counts_as_review(self, db: Session) -> None:
        """There is no way to publish something nobody looked at."""
        seller = make_seller(db)
        product = draft(db, seller)
        assert product.reviewed_at is None

        publish_product(db, seller, product.id)

        assert product.reviewed_at is not None

    def test_an_existing_review_time_is_not_overwritten(self, db: Session) -> None:
        """Unpublishing and republishing must not rewrite when it was checked."""
        seller = make_seller(db)
        product = draft(db, seller)
        publish_product(db, seller, product.id)
        first = product.reviewed_at

        unpublish_product(db, seller, product.id)
        publish_product(db, seller, product.id)

        assert product.reviewed_at == first

    def test_unpublishing_returns_it_to_draft(self, db: Session) -> None:
        """Not archived: a seller hiding something to fix a price expects to
        find it where they left it."""
        seller = make_seller(db)
        product = draft(db, seller)
        publish_product(db, seller, product.id)

        unpublish_product(db, seller, product.id)

        assert product.status == ProductStatus.DRAFT.value


class TestScoping:
    def test_another_sellers_product_cannot_be_published(self, db: Session) -> None:
        """
        Product ids are sequential. Without the seller scope, a guessed id would
        let anyone put a stranger's stock on sale.
        """
        mine = make_seller(db, slug="mine")
        theirs = make_seller(db, slug="theirs", display_name="Theirs")
        stranger = draft(db, theirs)

        with pytest.raises(PublishError, match="not in your shop"):
            publish_product(db, mine, stranger.id)

        assert stranger.status == ProductStatus.DRAFT.value

    def test_another_sellers_product_cannot_be_unpublished(self, db: Session) -> None:
        """The more damaging direction: taking a competitor's stock offline."""
        mine = make_seller(db, slug="mine")
        theirs = make_seller(db, slug="theirs", display_name="Theirs")
        stranger = draft(db, theirs)
        publish_product(db, theirs, stranger.id)

        with pytest.raises(PublishError):
            unpublish_product(db, mine, stranger.id)

        assert stranger.status == ProductStatus.PUBLISHED.value

    def test_another_sellers_product_cannot_be_read(self, db: Session) -> None:
        mine = make_seller(db, slug="mine")
        theirs = make_seller(db, slug="theirs", display_name="Theirs")
        stranger = draft(db, theirs)

        assert get_own_product(db, mine, stranger.id) is None
        assert get_own_product(db, theirs, stranger.id) is not None


class TestPublishingTheShop:
    def test_a_shop_needs_a_whatsapp_number(self, db: Session) -> None:
        """
        A live shop nobody can contact is a dead end for every buyer who reaches
        it — the whole funnel ends in silence.
        """
        seller = make_seller(db, whatsapp_number=None)

        with pytest.raises(PublishError, match="WhatsApp number"):
            publish_shop(db, seller)

        assert seller.is_published is False

    def test_publishing_opens_the_storefront(self, db: Session) -> None:
        seller = make_seller(db)

        publish_shop(db, seller)

        assert seller.is_published is True

    def test_a_shop_may_open_before_it_has_stock(self, db: Session) -> None:
        """
        Deliberately allowed. The storefront has an honest empty state, and
        refusing would mean explaining a rule instead of showing the seller the
        thing they are trying to build.
        """
        seller = make_seller(db)

        publish_shop(db, seller)

        assert seller.is_published is True

    def test_closing_keeps_the_slug(self, db: Session) -> None:
        """The slug has been shared. It stays; the shop simply 404s, exactly as
        an unknown slug does."""
        seller = make_seller(db, slug="nairobithrift")
        publish_shop(db, seller)

        unpublish_shop(db, seller)

        assert seller.is_published is False
        assert seller.slug == "nairobithrift"


class TestTheReviewQueue:
    def test_the_least_confident_parse_comes_first(self, db: Session) -> None:
        """
        The whole point of the ordering: a wrong price hides in the drafts the
        model was least sure about. Sorting by date would bury them.
        """
        seller = make_seller(db)
        draft(db, seller, title="Confident", parse_confidence=0.95, platform_post_id="a")
        draft(db, seller, title="Unsure", parse_confidence=0.20, platform_post_id="b")

        titles = [p.title for p in list_products(db, seller)]
        assert titles[0] == "Unsure"

    def test_hand_entered_products_sort_last(self, db: Session) -> None:
        """No confidence score means a human typed it. Nobody needs to
        re-check their own typing."""
        seller = make_seller(db)
        draft(db, seller, title="Typed", parse_confidence=None, platform_post_id="a")
        draft(db, seller, title="Guessed", parse_confidence=0.4, platform_post_id="b")

        titles = [p.title for p in list_products(db, seller)]
        assert titles == ["Guessed", "Typed"]

    def test_the_summary_separates_what_can_be_acted_on(self, db: Session) -> None:
        """
        "You have 30 drafts" is noise. "12 need a price" is a to-do list.
        """
        seller = make_seller(db)
        priced = draft(db, seller, platform_post_id="a")
        draft(db, seller, price_kes=None, platform_post_id="b")
        draft(db, seller, price_kes=None, platform_post_id="c")
        publish_product(db, seller, priced.id)

        summary = catalogue_summary(db, seller)

        assert summary == {"published": 1, "draft": 2, "needs_price": 2}

    def test_the_summary_is_scoped_to_the_seller(self, db: Session) -> None:
        mine = make_seller(db, slug="mine")
        theirs = make_seller(db, slug="theirs", display_name="Theirs")
        draft(db, theirs, platform_post_id="a")

        assert catalogue_summary(db, mine) == {"published": 0, "draft": 0, "needs_price": 0}


class TestAddingAProductByHand:
    """
    The fallback that never breaks: no scrape, no AI, no WhatsApp — a name and
    a price. It has to exist before any ingestion path does, or a seller cannot
    sell anything at all.
    """

    def test_a_name_is_enough(self, db: Session) -> None:
        """Everything else is optional. An unpriced product is a real draft."""
        seller = make_seller(db)

        product = create_product(db, seller, title="Mixed Ladies Sandals")

        assert product.title == "Mixed Ladies Sandals"
        assert product.price_kes is None
        assert product.status == ProductStatus.DRAFT.value

    def test_it_is_a_draft_even_though_a_human_typed_it(self, db: Session) -> None:
        """
        The publish gate is one act with one meaning. A second way to go live
        that the review queue never sees would defeat it.
        """
        seller = make_seller(db)

        product = create_product(db, seller, title="Cargo Pants", price_kes=1500)

        assert product.status == ProductStatus.DRAFT.value
        assert product.reviewed_at is not None

    def test_provenance_marks_it_as_never_re_syncable(self, db: Session) -> None:
        """
        THE RAIL THIS PROTECTS. A feed sync must never overwrite something a
        seller typed by hand — a seller who adds stock, syncs, and watches it
        vanish does not come back. The database pairs manual with upload and
        forbids a post id on either.
        """
        seller = make_seller(db)

        product = create_product(db, seller, title="Hand Typed")

        assert product.platform == Platform.MANUAL.value
        assert product.ingest_method == IngestMethod.UPLOAD.value
        assert product.platform_post_id is None

    def test_a_typed_price_is_attributed_to_the_seller(self, db: Session) -> None:
        """Never confused with something a model guessed."""
        seller = make_seller(db)

        product = create_product(db, seller, title="Bag", price_kes=2000)

        assert product.price_source == PriceSource.SELLER.value
        assert product.parse_confidence is None

    def test_a_blank_name_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(PublishError, match="name"):
            create_product(db, seller, title="   ")

    def test_a_zero_price_is_refused(self, db: Session) -> None:
        """KES 0 is always a mistake, never a giveaway — and blank is allowed
        for exactly the case a seller is unsure."""
        seller = make_seller(db)
        with pytest.raises(PublishError, match="more than zero"):
            create_product(db, seller, title="Free?", price_kes=0)

    def test_an_implausible_price_is_refused(self, db: Session) -> None:
        """Guards a slipped decimal point becoming a live price."""
        seller = make_seller(db)
        with pytest.raises(PublishError, match="number of zeros"):
            create_product(db, seller, title="Typo", price_kes=99_000_000)

    def test_a_pack_size_without_a_label_is_refused(self, db: Session) -> None:
        """ "KES 3,000 for 30" of what? The database pairs them; so does this."""
        seller = make_seller(db)
        with pytest.raises(PublishError, match="what"):
            create_product(db, seller, title="Bale", price_kes=3000, unit_quantity=30)

    def test_bulk_pricing_renders_the_way_the_seller_means_it(self, db: Session) -> None:
        """
        Found by spiking a real seller: @zumamitumbabales sells mitumba BALES.
        "KES 3,000" alone makes a buyer expect one pair.
        """
        seller = make_seller(db)

        product = create_product(
            db,
            seller,
            title="Mitumba Bale",
            price_kes=3000,
            unit_quantity=30,
            unit_label="pairs",
        )

        assert product.price_display == "KES 3,000 for 30 pairs"

    def test_sizes_are_split_but_not_normalised(self, db: Session) -> None:
        """Kenyan sizing varies by trade. Inventing a canonical form loses
        information the buyer needs."""
        seller = make_seller(db)

        product = create_product(db, seller, title="Sandals", sizes=" 38, 39 ,40 , ")

        assert product.sizes == ["38", "39", "40"]

    def test_zero_stock_is_allowed(self, db: Session) -> None:
        """It shows as "Sold out" rather than disappearing — a shop with
        nothing in it looks dead."""
        seller = make_seller(db)

        product = create_product(db, seller, title="Gone", price_kes=500, stock=0)

        assert product.stock == 0

    def test_a_hand_typed_product_can_be_published(self, db: Session) -> None:
        """End to end: the whole reason this exists."""
        seller = make_seller(db)
        product = create_product(db, seller, title="Sandals", price_kes=1500)

        publish_product(db, seller, product.id)

        assert product.status == ProductStatus.PUBLISHED.value
