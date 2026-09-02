"""
The publish gate — the human act that makes a product buyable.

    Product(draft) ──▶ publish_product() ──▶ Product(published) ──▶ storefront
                                 │
                          requires a price

    Seller(unpublished) ──▶ publish_shop() ──▶ Seller(published) ──▶ /shop/{slug}
                                 │
                          requires a WhatsApp number

WHY THIS MODULE EXISTS AT ALL, AND WHY IT DID NOT UNTIL NOW. "Publish is a human
gate" was written into three documents and enforced by two database constraints,
and **nothing in the application ever set a product to PUBLISHED**. The only
places that did were two lines in ``scripts/seed_demo_shop.py``. A real seller
could sign up, connect M-Pesa, add stock, and reach a 404 storefront — the rule
existed, the gate did not.

THE AGENT PROPOSES, CODE DISPOSES. Ingestion and the vision cascade write
DRAFT, always. Nothing automatic may promote a product: a model that guessed a
price wrong is a mistake, and the same mistake published is a seller taking
money for the wrong number. A person confirms, here, deliberately.

WHY THE ERRORS ARE RAISED RATHER THAN LEFT TO POSTGRES. The rails are real —
``published_requires_price`` and ``published_needs_whatsapp`` would refuse these
writes anyway, and they must stay. But an IntegrityError reaching a seller says
nothing they can act on. These checks exist to produce a sentence that names the
fix; the constraints exist so no future code path can skip it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    IngestMethod,
    Platform,
    PriceSource,
    Product,
    ProductStatus,
    Seller,
)
from app.services.catalog import CATALOG_SYNC
from app.services.jobs import enqueue


class PublishError(Exception):
    """Publishing was refused, with a message safe to show the seller."""


def _sync_catalogue(db: Session, product: Product) -> None:
    """
    Queue a Meta-catalogue sync for a product whose buyability just changed.

    Off the request path — it is a Graph call, and publishing must not wait on
    Meta. Deduped per product, so a flurry of edits collapses to one sync. Does
    nothing costly when no catalogue is configured: the job runs, finds no
    ``WHATSAPP_CATALOG_ID``, and returns.
    """
    enqueue(
        db,
        CATALOG_SYNC,
        payload={"product_id": product.id},
        seller_id=product.seller_id,
        dedupe_key=f"catalog:{product.id}",
    )


def get_own_product(db: Session, seller: Seller, product_id: int) -> Product | None:
    """
    One of this seller's products, whatever its status.

    Scoped to the seller for the same reason the storefront is: product ids are
    sequential, and without it a guessed id would let one seller publish,
    unpublish or reprice another's stock.
    """
    return db.scalar(
        select(Product).where(Product.id == product_id, Product.seller_id == seller.id)
    )


def list_products(db: Session, seller: Seller, status: str | None = None) -> list[Product]:
    """
    The seller's catalogue, for the review queue.

    Ordered with the LOWEST parse confidence first, then newest. That ordering
    is the point of the screen: a wrong price hides in the drafts the model was
    least sure about, and sorting by date would bury them under whatever
    arrived most recently. Hand-entered products have no confidence score and
    sort last, which is right — nobody needs to re-check their own typing.
    """
    query = select(Product).where(Product.seller_id == seller.id)
    if status:
        query = query.where(Product.status == status)

    return list(
        db.scalars(
            query.order_by(
                Product.parse_confidence.asc().nulls_last(),
                Product.created_at.desc(),
            )
        ).all()
    )


#: A product a seller typed in themselves is capped here. Not a business rule —
#: a guard against a slipped decimal point becoming a live price. The database
#: allows up to 10,000,000; this is the form's own, tighter, sanity check.
MAX_MANUAL_PRICE_KES = 10_000_000


def create_product(
    db: Session,
    seller: Seller,
    *,
    title: str,
    price_kes: int | None = None,
    stock: int = 1,
    description: str | None = None,
    category: str | None = None,
    sizes: str | None = None,
    unit_quantity: int | None = None,
    unit_label: str | None = None,
) -> Product:
    """
    Add a product the seller typed in themselves.

    Args:
        db: Session.
        seller: Whose shop.
        title: What the buyer sees. The only required field.
        price_kes: Whole shillings. May be omitted — an unpriced product is a
            legitimate draft, it simply cannot be published.
        stock: How many. Zero is allowed; it shows as "Sold out".
        description: Free text.
        category: The seller's own grouping, for the storefront pills.
        sizes: Comma-separated, kept exactly as typed.
        unit_quantity: How many items one purchase contains — 30 for a bale.
        unit_label: What those units are called: "pairs", "pieces".

    Returns:
        The created product, always as a DRAFT.

    Raises:
        PublishError: On a missing title, a nonsensical price or stock, or a
            unit quantity given without a label.

    Notes:
        CREATED AS A DRAFT, ALWAYS, even though a human typed every field. The
        publish gate is one act with one meaning; letting this path skip it
        would create a second way to go live that the review queue never sees.

        PROVENANCE IS ``manual`` + ``upload``, which the database requires to
        agree — a "tiktok upload" is incoherent, and ``upload_has_no_post_id``
        keeps this product from ever looking re-syncable. That matters: a feed
        sync must never overwrite something a seller typed by hand.
    """
    clean_title = title.strip()
    if not clean_title:
        raise PublishError("Give the item a name.")

    if price_kes is not None:
        if price_kes <= 0:
            raise PublishError("A price must be more than zero. Leave it blank if you're unsure.")
        if price_kes > MAX_MANUAL_PRICE_KES:
            raise PublishError("That price looks like a mistake. Check the number of zeros.")

    if stock < 0:
        raise PublishError("Stock cannot be negative.")

    # The database pairs these two, because "KES 3,000 for 30" of what?
    if (unit_quantity is None) != (unit_label is None or not unit_label.strip()):
        raise PublishError(
            "If one purchase contains several items, give both the number and what "
            "they are called — for example 30 and “pairs”."
        )
    if unit_quantity is not None and unit_quantity < 1:
        raise PublishError("A pack must contain at least one item.")

    product = Product(
        seller_id=seller.id,
        title=clean_title,
        price_kes=price_kes,
        stock=stock,
        description=(description or "").strip() or None,
        category=(category or "").strip() or None,
        sizes=[s.strip() for s in (sizes or "").split(",") if s.strip()],
        unit_quantity=unit_quantity,
        unit_label=(unit_label or "").strip() or None,
        # Provenance the constraints require to agree, and which stops any
        # future feed sync from touching this row.
        platform=Platform.MANUAL.value,
        ingest_method=IngestMethod.UPLOAD.value,
        status=ProductStatus.DRAFT.value,
        # A human typed it, so it has been reviewed by definition. It still has
        # to be published deliberately.
        reviewed_at=datetime.now(UTC),
        price_source=PriceSource.SELLER.value if price_kes is not None else None,
    )
    db.add(product)
    db.flush()
    return product


def publish_product(db: Session, seller: Seller, product_id: int) -> Product:
    """
    Make one product buyable.

    Args:
        db: Session.
        seller: The owner. Enforced, not assumed.
        product_id: Which product.

    Returns:
        The published product.

    Raises:
        PublishError: If the product is not this seller's, or has no price.

    Notes:
        Sets ``reviewed_at`` if it is not already set. Publishing IS the human
        review — the two were separate fields precisely so a seller could
        confirm a draft without listing it, but the reverse cannot happen: there
        is no way to publish something nobody looked at.
    """
    product = get_own_product(db, seller, product_id)
    if product is None:
        raise PublishError("That item is not in your shop.")

    if product.price_kes is None:
        raise PublishError(
            f"“{product.title}” needs a price before it can go live. Add one and publish again."
        )

    product.status = ProductStatus.PUBLISHED.value
    if product.reviewed_at is None:
        product.reviewed_at = datetime.now(UTC)
    db.flush()
    _sync_catalogue(db, product)
    return product


def unpublish_product(db: Session, seller: Seller, product_id: int) -> Product:
    """
    Take a product off the storefront, keeping it and its history.

    Back to DRAFT rather than ARCHIVED: a seller hiding something while they fix
    a price expects to find it again in the same place. Archiving is a separate,
    more final act.

    Orders already placed are untouched — they copied everything they need.
    """
    product = get_own_product(db, seller, product_id)
    if product is None:
        raise PublishError("That item is not in your shop.")

    product.status = ProductStatus.DRAFT.value
    db.flush()
    _sync_catalogue(db, product)
    return product


def publish_shop(db: Session, seller: Seller) -> Seller:
    """
    Open the storefront to buyers.

    Raises:
        PublishError: If the seller has no WhatsApp number. A live shop nobody
            can contact is a dead end for every buyer who reaches it — the whole
            funnel ends in silence — which is why the database refuses it too.

    Notes:
        Deliberately does NOT require a published product. A seller who opens
        their shop before listing anything gets an empty-state page that says
        so, which is honest; refusing would mean explaining a rule instead of
        showing them the thing they are trying to build.
    """
    if not seller.whatsapp_number:
        raise PublishError(
            "Add your WhatsApp number before opening your shop — buyers use it to reach you."
        )

    seller.is_published = True
    db.flush()
    return seller


def unpublish_shop(db: Session, seller: Seller) -> Seller:
    """
    Close the storefront. The slug is kept, because it has been shared.

    ``/shop/{slug}`` then 404s exactly as an unknown slug does — a closed shop
    and a shop that never existed are indistinguishable from outside, which is
    the same rule the storefront already applies to unpublished sellers.
    """
    seller.is_published = False
    db.flush()
    return seller


def catalogue_summary(db: Session, seller: Seller) -> dict[str, int]:
    """
    Counts for the dashboard: how much is live, and how much is waiting.

    Returns:
        ``{"published": n, "draft": n, "needs_price": n}``.

        ``needs_price`` is the number a seller can actually act on — drafts that
        cannot be published until someone types a number. It is counted
        separately from ``draft`` because "you have 30 drafts" is noise, while
        "12 need a price" is a to-do list.
    """
    rows = db.execute(
        select(Product.status, func.count(Product.id))
        .where(Product.seller_id == seller.id)
        .group_by(Product.status)
    ).all()
    # Built explicitly rather than `dict(rows)`: SQLAlchemy yields Row objects,
    # which dict() accepts at runtime and mypy rejects — and the loop says what
    # the pair means.
    counts: dict[str, int] = {}
    for status_value, count in rows:
        counts[status_value] = count

    needs_price = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.DRAFT.value,
                Product.price_kes.is_(None),
            )
        )
        or 0
    )

    return {
        "published": counts.get(ProductStatus.PUBLISHED.value, 0),
        "draft": counts.get(ProductStatus.DRAFT.value, 0),
        "needs_price": needs_price,
    }
