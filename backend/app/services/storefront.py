"""
What a buyer is allowed to see, and the WhatsApp handoff.

    /shop/{slug}       ──> published, in-stock-first products
    /shop/{slug}/{id}  ──> one product + a pre-filled WhatsApp link

WHY A SERVICE RATHER THAN QUERIES IN THE ROUTE: "what is publicly visible" is a
rule, and rules belong somewhere they can be tested once and reused. A route
that builds its own query is a route that can forget the seller is unpublished.

THE HANDOFF IS THE PRODUCT. Everything upstream — scraping, the price cascade,
the review queue — exists so this message can be written for the buyer instead
of by them. The pre-filled text names the item and its price, so the seller
opens a chat already knowing what is being asked about.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Account, Platform, Product, ProductStatus, Seller

#: wa.me needs the number in E.164 with no + and no punctuation.
_WA_BASE = "https://wa.me/"


def get_public_shop(db: Session, slug: str) -> Seller | None:
    """
    Load a shop a buyer is allowed to see.

    Args:
        db: Session.
        slug: The URL segment.

    Returns:
        The seller, or None when the slug is unknown OR the shop is
        unpublished. Both are a 404 to the buyer: an unpublished shop is full of
        half-parsed drafts, and showing it would embarrass the seller.
    """
    return db.scalar(select(Seller).where(Seller.slug == slug, Seller.is_published.is_(True)))


def shop_cover(db: Session, seller: Seller) -> str | None:
    """
    The image a chat app should show when this shop's link is pasted.

    Args:
        db: Session.
        seller: The shop.

    Returns:
        A stored path or absolute URL, or None when the shop has nothing to show.

    Notes:
        THE LINK PREVIEW IS THE SHOPFRONT. This product's entire distribution is
        a URL pasted into a WhatsApp chat, a status or a bio — so the card Meta
        draws around it IS the shop window, seen by far more people than ever
        tap through. A card with no image is a grey rectangle with a domain in
        it, and it reads as a link to something broken.

        A seller's avatar is better when they have one, because it is their
        brand. Almost none do yet, so we fall back to the newest thing they are
        actually selling — which is a photograph of stock, and a far better
        advert than nothing.
    """
    if seller.avatar_url:
        return seller.avatar_url

    return db.scalar(
        select(Product.cover_url)
        .where(
            Product.seller_id == seller.id,
            Product.status == ProductStatus.PUBLISHED.value,
            Product.cover_url.is_not(None),
        )
        .order_by(Product.created_at.desc())
        .limit(1)
    )


def get_own_shop(db: Session, slug: str, account: Account | None) -> Seller | None:
    """
    A closed shop, but only for the person who owns it.

    Args:
        db: Session.
        slug: The URL segment.
        account: Whoever is signed in, or None.

    Returns:
        The seller when this account owns that slug; None otherwise — including
        for a signed-in seller looking at somebody else's closed shop.

    Notes:
        WHY A SELLER MUST BE ABLE TO SEE THEIR OWN CLOSED SHOP. "View store" is
        the only way to answer "what will a buyer actually get?", and it is most
        needed BEFORE opening, which is exactly when the public route 404s. A
        seller who cannot preview has to publish blind and find out from a
        customer.

        THIS IS NOT A HOLE IN THE PUBLISH GATE. It widens who may LOOK, never
        what is shown: the page still lists only published products, because the
        question being answered is what a buyer sees, not what exists. Ownership
        is proved by the session, so a stranger with the URL still gets a 404.
    """
    if account is None or account.seller is None:
        return None
    if account.seller.slug != slug:
        return None
    return account.seller


#: Sort orders the shop page offers, and the ORDER BY each one means.
#:
#: A whitelist rather than a column name from the query string: the value
#: arrives from a URL a stranger controls, and "sort by this column" is one
#: careless f-string away from being "run this SQL".
SORT_OPTIONS = {
    "newest": "Newest first",
    "price_low": "Price: low to high",
    "price_high": "Price: high to low",
}
DEFAULT_SORT = "newest"


def get_public_products(
    db: Session,
    seller: Seller,
    category: str | None = None,
    search: str | None = None,
    sort: str | None = None,
    max_price_kes: int | None = None,
) -> list[Product]:
    """
    The shop's visible catalogue, optionally filtered.

    Sold-out items are INCLUDED, deliberately. A buyer who saw the item on
    TikTok and finds nothing here assumes the shop is dead; a "Sold out" badge
    tells them it is real and worth asking about. It also gives Soko Intel a
    restock signal later.

    Ordered in-stock first, then newest — because the first screen is the only
    one many buyers see.

    Args:
        db: Session.
        seller: The shop.
        category: Restrict to one category pill. None or "" means all.
        search: Free text matched against title and description.
        sort: One of ``SORT_OPTIONS``. Anything else falls back to newest,
            silently — a buyer who edits the URL gets a sane page, not a 500.
        max_price_kes: Ceiling, inclusive. Added for the chat, where a buyer
            types "anything under 1000" — a question people genuinely ask a
            shopkeeper and could not previously ask here.

    Returns:
        Published products, filtered and ordered.
    """
    query = select(Product).where(
        Product.seller_id == seller.id,
        Product.status == ProductStatus.PUBLISHED.value,
    )

    if category:
        query = query.where(Product.category == category)

    if search and search.strip():
        # ILIKE, not a tsvector index: a shop holds tens of products, not
        # millions, and a full-text index would be machinery bought with
        # complexity we would never earn back. `%` and `_` are escaped so a
        # buyer typing "50% off" searches for that rather than matching
        # everything.
        term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        query = query.where(
            or_(
                Product.title.ilike(pattern),
                Product.description.ilike(pattern),
            )
        )

    if max_price_kes is not None:
        # UNPRICED ITEMS ARE EXCLUDED, not treated as free. A draft that never
        # got a price is not "under 1000"; it is unknown, and putting it in
        # front of somebody who asked about their budget answers a question
        # they did not ask.
        query = query.where(
            Product.price_kes.is_not(None),
            Product.price_kes <= max_price_kes,
        )

    # In-stock always leads, whatever the sort. A sold-out item at the top of a
    # price-sorted list is the cheapest thing the buyer cannot have.
    ordering: list[Any] = [(Product.stock > 0).desc()]
    if sort == "price_low":
        ordering.append(Product.price_kes.asc().nulls_last())
    elif sort == "price_high":
        ordering.append(Product.price_kes.desc().nulls_last())
    ordering.append(Product.created_at.desc())

    return list(db.scalars(query.order_by(*ordering)).all())


def get_categories(db: Session, seller: Seller) -> list[str]:
    """
    The category pills for this shop's header.

    Built from what this seller actually typed rather than a fixed taxonomy —
    a shop selling only shoes should not display four empty pills, and a shop
    selling something we never anticipated should not be unlabelable.

    Returns:
        Distinct non-empty categories on published products, alphabetically.
    """
    rows = db.scalars(
        select(Product.category)
        .where(
            Product.seller_id == seller.id,
            Product.status == ProductStatus.PUBLISHED.value,
            Product.category.is_not(None),
            Product.category != "",
        )
        .distinct()
        .order_by(Product.category)
    ).all()
    return [row for row in rows if row]


def get_public_product(db: Session, seller: Seller, product_id: int) -> Product | None:
    """
    One product, scoped to this shop.

    Scoping matters: without it, a guessed id would show another seller's
    product under this seller's header, complete with the wrong WhatsApp number.
    """
    return db.scalar(
        select(Product).where(
            Product.id == product_id,
            Product.seller_id == seller.id,
            Product.status == ProductStatus.PUBLISHED.value,
        )
    )


def build_shop_whatsapp_url(seller: Seller) -> str | None:
    """
    The "Chat with this seller" link on the shop page.

    Deliberately vaguer than the product version: a buyer on the shop page has
    not chosen anything yet, so naming an item would put words in their mouth.
    It still says which shop, because a seller running two of them needs to know
    which one the buyer is standing in.

    Returns:
        A ready link, or None when the seller has no number — the page must then
        hide the button rather than show one that goes nowhere.
    """
    if not seller.whatsapp_number:
        return None

    message = f"Hi {seller.display_name}! I'm browsing your Biasharamall shop."
    return f"{_WA_BASE}{seller.whatsapp_number}?text={quote(message)}"


def build_whatsapp_url(seller: Seller, product: Product) -> str | None:
    """
    A wa.me link with the message already written.

    Args:
        seller: The shop, for its WhatsApp number.
        product: What the buyer is asking about.

    Returns:
        A ready link, or None when the seller has no number — in which case the
        page must hide the button rather than show one that goes nowhere.

    Notes:
        The message includes the PRICE AS DISPLAYED, units and all. If the
        storefront says "KES 3,000 for 30 pairs" and the message says
        "KES 3,000", the seller and buyer start the conversation disagreeing
        about what was offered.
    """
    if not seller.whatsapp_number:
        return None

    price = product.price_display or "the price"
    message = (
        f"Hi {seller.display_name}! I saw {product.title} "
        f"({price}) on your Biasharamall shop. Is it available?"
    )

    return f"{_WA_BASE}{seller.whatsapp_number}?text={quote(message)}"


def connected_account(seller: Seller, platform: Platform = Platform.TIKTOK) -> object | None:
    """
    The seller's verified account on a platform, for the shop header.

    Anything returned here is verified by construction — unverified accounts
    cannot exist — so the storefront may show it as confirmed without checking.
    """
    return seller.account_for(platform)
