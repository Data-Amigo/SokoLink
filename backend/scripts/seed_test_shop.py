"""
Fill a shop with browsable stock, so the buyer journey can be walked end to end.

    python scripts/seed_test_shop.py [slug]

    an existing seller ──▶ payment method ──▶ published products ──▶ /shop/<slug>

WHY THIS EXISTS ALONGSIDE ``seed_demo_shop.py``. That one scrapes TikTok through
Apify and stores its own copies of the covers — it proves the ingestion pipeline.
This one proves nothing about ingestion and does not try to: it exists so there
is something to LOOK at when testing the storefront, the cart, checkout and the
WhatsApp in-app browser. No Apify token, no network fetch, no media directory.

COVERS ARE ABSOLUTE URLS, DELIBERATELY. ``media.public_url`` passes an absolute
URL through untouched, so these render from any host without the deployment
needing a writable media directory — which Railway does not durably have. They
are placeholder photographs, not the seller's real stock, and they are meant to
be replaced by real ones before anybody shows this to a seller.

IT DOES NOT PUBLISH THE SHOP. Products land PUBLISHED so the storefront has
something to show, but opening the shop stays a human act performed in the
workspace. Publish is a gate with one meaning; a script that flipped it would
create a second way to go live and would skip the very screen we want tested.

Idempotent by title: running it twice tops the same products up rather than
creating duplicates. Leaves every other seller alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    IngestMethod,
    PaymentMethod,
    PaymentMethodKind,
    Platform,
    Product,
    ProductStatus,
    Seller,
)

#: Which shop to fill when none is named on the command line.
DEFAULT_SLUG = "vitabu-bora"

#: THE TWILIO SANDBOX NUMBER, on purpose.
#:
#: "Chat with this seller" builds a wa.me link from this, so tapping it opens
#: the bot that now answers — which makes the storefront's contact button a real
#: part of the test rather than a dead end. It must never be a number scraped
#: from a real seller's bio: that would point every tester at a stranger's
#: phone. Change it in the workspace under Payments.
SANDBOX_WHATSAPP = "14155238886"


#: Placeholder covers. Deterministic per seed word, so a product keeps the same
#: photograph across runs instead of shuffling every time the page loads.
def _cover(seed: str) -> str:
    """A stable placeholder photograph for one product."""
    return f"https://picsum.photos/seed/{seed}/800/800"


#: (title, price, was-price, category, sizes, stock, cover seed)
#:
#: Prices are realistic Nairobi figures and a couple carry a "was" price, so the
#: storefront's discount badge has something to draw. Categories repeat across
#: products because the shop page groups by them into filter pills — one product
#: per category would make the row look broken.
CATALOGUE: list[tuple[str, int, int | None, str, list[str], int, str]] = [
    ("Ankara Print Shirt", 1800, 2200, "Fashion", ["S", "M", "L", "XL"], 12, "ankara"),
    ("Leather Handbag", 3500, 4200, "Bags", [], 5, "handbag"),
    ("Canvas Sneakers", 2400, None, "Shoes", ["39", "40", "41", "42", "43"], 9, "sneakers"),
    ("Maasai Beaded Sandals", 1200, 1500, "Shoes", ["37", "38", "39", "40"], 15, "sandals"),
    ("Kitenge Wrap Dress", 2800, None, "Fashion", ["S", "M", "L"], 7, "kitenge"),
    ("Shea Butter Set", 950, 1200, "Beauty", [], 20, "shea"),
    ("Woven Tote Basket", 1600, None, "Bags", [], 11, "tote"),
    ("Denim Jacket", 3200, 3900, "Fashion", ["M", "L", "XL"], 4, "denim"),
]


def main(slug: str) -> int:
    """
    Fill one shop with stock it can actually sell.

    Args:
        slug: The seller to fill.

    Returns:
        A process exit code: 0 on success, 1 when the slug is unknown.
    """
    db = SessionLocal()
    try:
        seller = db.scalar(select(Seller).where(Seller.slug == slug))
        if seller is None:
            known = [s.slug for s in db.scalars(select(Seller)).all()]
            print(f"No shop with slug {slug!r}.\nShops in this database: {', '.join(known)}")
            return 1

        print(f"Filling {seller.display_name} (/shop/{seller.slug})\n")

        # A shop cannot be opened without a contact number — the database
        # refuses it, because a live shop nobody can reach is a dead end.
        if not seller.whatsapp_number:
            seller.whatsapp_number = SANDBOX_WHATSAPP
            print(f"  whatsapp number  -> {SANDBOX_WHATSAPP} (Twilio sandbox)")

        # Checkout is blocked without somewhere for the money to go. Pochi,
        # because it needs no credentials and is what most sellers actually run.
        method = db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id))
        if method is None:
            db.add(
                PaymentMethod(
                    seller_id=seller.id,
                    kind=PaymentMethodKind.POCHI.value,
                    number=SANDBOX_WHATSAPP,
                    account_name=seller.display_name,
                )
            )
            print("  payment method   -> Pochi la Biashara (manual confirmation)")

        added = updated = 0
        for title, price, was, category, sizes, stock, seed in CATALOGUE:
            product = db.scalar(
                select(Product).where(Product.seller_id == seller.id, Product.title == title)
            )
            if product is None:
                # manual + upload, which the database requires to agree: a
                # "tiktok upload" is incoherent, and it keeps a future feed sync
                # from ever overwriting these.
                product = Product(
                    seller_id=seller.id,
                    title=title,
                    platform=Platform.MANUAL.value,
                    ingest_method=IngestMethod.UPLOAD.value,
                )
                db.add(product)
                added += 1
            else:
                updated += 1

            product.price_kes = price
            product.compare_at_price_kes = was
            product.category = category
            product.sizes = sizes
            product.stock = stock
            product.cover_url = _cover(seed)
            product.description = f"{title} — sample stock for testing the storefront."
            product.status = ProductStatus.PUBLISHED.value

        db.commit()

        print(f"\n  products         -> {added} added, {updated} refreshed")
        print(f"\nShop is {'OPEN' if seller.is_published else 'STILL CLOSED'}.")
        if not seller.is_published:
            print("  Open it yourself from /products — publishing stays a human act.")
        print(f"\n  {settings.app_base_url}/shop/{seller.slug}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SLUG))
