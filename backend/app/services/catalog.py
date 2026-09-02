"""
A shop's published products, mirrored in a Meta Commerce catalogue.

    Product (published) ──▶ catalog item (retailer_id = soko_<id>) ──▶ MPM card

WHY A CATALOGUE AT ALL. A Multi-Product Message shows real product cards a buyer
taps into a native WhatsApp cart, but every card is a reference — a retailer_id
into a catalogue Meta holds, not data in the message. So a product must exist in
that catalogue before it can appear in a message, and it must be kept in step:
a price change, a sell-out, an unpublish. This module is that mapping and that
sync. The raw Graph calls live in ``whatsapp_cloud``; the sync runs on the
worker (kind ``catalog_sync``), off the request path, because it is a network
call and publishing should not wait on Meta.

RETAILER_ID IS DERIVED, NOT STORED. ``soko_<product.id>`` is stable, unique, and
needs no column and no migration — and it reverses cleanly, so an inbound order
that names ``soko_42`` resolves straight back to product 42.
"""

from __future__ import annotations

from app.config import settings
from app.models import Product, ProductStatus
from app.services import whatsapp_cloud
from app.services.media import absolute_url

#: The worker job kind that syncs one product to the catalogue.
CATALOG_SYNC = "catalog_sync"

#: Prefix on every retailer_id, so ours never collide with a seller's own SKUs
#: if a catalogue is ever shared.
_RETAILER_PREFIX = "soko_"


def retailer_id(product: Product) -> str:
    """The catalogue id for a product — stable, unique, needs no column."""
    return f"{_RETAILER_PREFIX}{product.id}"


def product_id_from_retailer(rid: str) -> int | None:
    """Reverse :func:`retailer_id`, or None if the string is not one of ours."""
    if not rid.startswith(_RETAILER_PREFIX):
        return None
    tail = rid[len(_RETAILER_PREFIX) :]
    return int(tail) if tail.isdigit() else None


def item_data(product: Product) -> dict[str, object]:
    """
    One product as the catalogue item fields Meta expects.

    Notes:
        PRICE IS IN MINOR UNITS. Meta's catalogue takes an integer in the
        currency's smallest unit, so 1,800 KES is 180000. Money here is integer
        KES, so the conversion is a clean ``* 100`` with nothing to round.

        THE IMAGE MUST BE A PUBLIC URL. ``absolute_url`` builds it from
        ``app_base_url``; on localhost Meta cannot reach it, which is a
        deployment fact, not a bug here.
    """
    seller = product.seller
    shop_url = f"{settings.app_base_url}/shop/{seller.slug}" if seller else settings.app_base_url
    return {
        "retailer_id": retailer_id(product),
        "name": (product.title or "Item")[:200],
        "description": (product.description or product.title or "")[:9999],
        "availability": "in stock" if product.stock > 0 else "out of stock",
        "condition": "new",
        "price": (product.price_kes or 0) * 100,
        "currency": "KES",
        "image_url": absolute_url(product.cover_url) or "",
        "url": shop_url,
    }


def sync_product(product: Product) -> bool:
    """
    Bring one product's catalogue entry in line with its current state.

    A published product with a price is upserted; anything else (a draft, an
    archived item, one whose price was cleared) is removed, so a card can never
    outlive the thing it sells.

    Returns:
        True if a Graph call was made, False when no catalogue is configured
        (the native surface is simply off) — so a caller can tell "synced" from
        "nothing to sync".
    """
    catalog_id = settings.whatsapp_catalog_id
    if not catalog_id:
        return False

    live = product.status == ProductStatus.PUBLISHED.value and product.price_kes is not None
    if live:
        whatsapp_cloud.catalog_upsert(catalog_id, [item_data(product)])
    else:
        whatsapp_cloud.catalog_delete(catalog_id, [retailer_id(product)])
    return True
