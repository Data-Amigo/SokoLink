"""
What a buyer is allowed to see, and the WhatsApp handoff.

    /{slug}            ──> published, in-stock-first products
    /{slug}/{id}       ──> one product + a pre-filled WhatsApp link

WHY A SERVICE RATHER THAN QUERIES IN THE ROUTE: "what is publicly visible" is a
rule, and rules belong somewhere they can be tested once and reused. A route
that builds its own query is a route that can forget the seller is unpublished.

THE HANDOFF IS THE PRODUCT. Everything upstream — scraping, the price cascade,
the review queue — exists so this message can be written for the buyer instead
of by them. The pre-filled text names the item and its price, so the seller
opens a chat already knowing what is being asked about.
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Platform, Product, ProductStatus, Seller

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


def get_public_products(db: Session, seller: Seller) -> list[Product]:
    """
    The shop's visible catalogue.

    Sold-out items are INCLUDED, deliberately. A buyer who saw the item on
    TikTok and finds nothing here assumes the shop is dead; a "Sold out" badge
    tells them it is real and worth asking about. It also gives Soko Intel a
    restock signal later.

    Ordered in-stock first, then newest — because the first screen is the only
    one many buyers see.
    """
    return list(
        db.scalars(
            select(Product)
            .where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.PUBLISHED.value,
            )
            .order_by(
                (Product.stock > 0).desc(),
                Product.created_at.desc(),
            )
        ).all()
    )


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
        f"({price}) on your Biashara Mall shop. Is it available?"
    )

    return f"{_WA_BASE}{seller.whatsapp_number}?text={quote(message)}"


def connected_account(seller: Seller, platform: Platform = Platform.TIKTOK) -> object | None:
    """
    The seller's verified account on a platform, for the shop header.

    Anything returned here is verified by construction — unverified accounts
    cannot exist — so the storefront may show it as confirmed without checking.
    """
    return seller.account_for(platform)
