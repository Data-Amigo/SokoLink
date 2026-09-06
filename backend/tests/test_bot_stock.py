"""
'my products' / 'stock' shows the live catalogue, not the pricing queue.

THE REGRESSION THIS GUARDS. Those words used to route to the pricing queue,
which only knows DRAFTS that have a price. A seller with a full shop and an empty
queue was told "All done ✅ — send another photo" — which reads as "you have
nothing". The catalogue view answers the question actually asked: what is in my
shop right now. 'drafts' stays the queue, and that split is what these protect.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import ProductStatus
from app.services.bot import handle
from tests.factories import make_product, make_seller

PHONE = "254712345678"


def _seller(db: Session, **overrides: Any):
    return make_seller(db, whatsapp_number=PHONE, **overrides)


def _published(db: Session, seller: Any, title: str, price: int, stock: int, i: int):
    return make_product(
        db,
        seller,
        title=title,
        status=ProductStatus.PUBLISHED.value,
        price_kes=price,
        stock=stock,
        platform_post_id=f"710000000000000{i:04d}",
    )


def _say(db: Session, text: str) -> str:
    return "\n".join(r.body for r in handle(db, PHONE, text).replies)


class TestStockView:
    def test_my_products_lists_the_live_catalogue(self, db: Session) -> None:
        seller = _seller(db, is_published=True)
        _published(db, seller, "Leather Sandals", 1200, 5, 1)
        _published(db, seller, "Ankara Dress", 2500, 4, 2)

        said = _say(db, "my products")

        assert "Leather Sandals" in said
        assert "Ankara Dress" in said
        # The old bug: the pricing-queue "all done" message.
        assert "All done" not in said

    def test_stock_items_and_inventory_are_the_same_view(self, db: Session) -> None:
        seller = _seller(db, is_published=True)
        _published(db, seller, "Leather Sandals", 1200, 5, 1)

        for word in ("stock", "items", "my stock", "inventory"):
            assert "Leather Sandals" in _say(db, word), word

    def test_sold_out_and_low_stock_are_marked(self, db: Session) -> None:
        seller = _seller(db, is_published=True)
        _published(db, seller, "Sling Bag", 1500, 0, 1)
        _published(db, seller, "Ankara Dress", 2500, 2, 2)

        said = _say(db, "stock")

        assert "sold out" in said.lower()
        assert "only 2 left" in said

    def test_empty_shop_invites_a_first_photo(self, db: Session) -> None:
        _seller(db)

        said = _say(db, "my products")

        assert "haven't added anything yet" in said
        assert "All done" not in said

    def test_drafts_still_shows_the_pricing_queue(self, db: Session) -> None:
        # A priced draft is the queue's business; the catalogue view is separate.
        seller = _seller(db)
        make_product(
            db,
            seller,
            title="New Tee",
            price_kes=800,
            platform_post_id="710000000000000999",
        )

        said = _say(db, "drafts")

        assert "Ready to go live" in said
        assert "New Tee" in said
