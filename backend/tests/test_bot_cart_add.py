"""
Adding an item shows a basket that INCLUDES it — never one item behind.

THE BUG THIS GUARDS. add_item wrote the cart row and flushed it, but left the
cart's already-loaded ``items`` collection stale — so the "Added ✅" confirmation
rendered the basket minus the item just added. The very first add read the
contradiction "Added ✅ … your basket is empty", and a second add showed only the
first item.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ProductStatus
from app.services.bot import handle
from tests.factories import make_product, make_seller

PHONE = "254701234567"


def _say(db: Session, text: str) -> str:
    return "\n".join(r.body for r in handle(db, PHONE, text).replies)


def _shop(db: Session) -> None:
    seller = make_seller(db, slug="kicks", is_published=True)
    make_product(
        db,
        seller,
        title="Beaded Necklace",
        category="Beauty",
        status=ProductStatus.PUBLISHED.value,
        price_kes=800,
        stock=10,
        platform_post_id="710000000000010001",
    )
    make_product(
        db,
        seller,
        title="Canvas Sneakers",
        category="Shoes",
        status=ProductStatus.PUBLISHED.value,
        price_kes=1800,
        stock=5,
        sizes=["40", "41"],
        platform_post_id="710000000000010002",
    )


def test_first_add_shows_the_item_not_an_empty_basket(db: Session) -> None:
    _shop(db)
    _say(db, "shop kicks")
    _say(db, "cat:Beauty")
    _say(db, "1")

    said = _say(db, "add")

    assert "Beaded Necklace" in said
    assert "800" in said
    assert "empty" not in said.lower()


def test_second_add_shows_both_items_and_the_right_total(db: Session) -> None:
    _shop(db)
    _say(db, "shop kicks")
    _say(db, "cat:Beauty")
    _say(db, "1")
    _say(db, "add")
    _say(db, "cat:Shoes")
    _say(db, "1")
    _say(db, "add")

    said = _say(db, "41")  # picking the size is what commits the sneakers

    assert "Beaded Necklace" in said
    assert "Canvas Sneakers" in said
    assert "2,600" in said
