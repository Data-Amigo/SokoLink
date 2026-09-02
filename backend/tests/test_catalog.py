"""
The native catalogue surface: sync, Multi-Product Messages, and the order back.

A published product is mirrored into a Meta Commerce catalogue so it can appear
as a tappable card; a buyer taps cards into a WhatsApp cart and sends it back,
which arrives as an ``order`` message and lands in the ordinary checkout. These
prove the mapping, the sync (off the request path), the message the buyer sees,
and the order coming home — all without a real Graph call.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import ConversationState, Job, Product, ProductStatus
from app.services import catalog, whatsapp_cloud
from app.services.bot import get_conversation, handle, handle_order
from app.services.catalog import CATALOG_SYNC, product_id_from_retailer, retailer_id
from app.services.catalogue import publish_product
from tests.factories import make_payment_method, make_product, make_seller

SELLER_PHONE = "254712345678"
BUYER_PHONE = "254799999999"


def _published(db: Session, seller: Any, **overrides: Any) -> Product:
    values: dict[str, Any] = {
        "status": ProductStatus.PUBLISHED.value,
        "price_kes": 500,
        "stock": 3,
        "title": "Ankara Shirt",
    }
    values.update(overrides)
    return make_product(db, seller, **values)


@pytest.fixture
def catalogue_on(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A configured catalogue, with the Graph calls captured instead of sent."""
    monkeypatch.setattr(get_settings(), "whatsapp_catalog_id", "cat_123")
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        whatsapp_cloud, "catalog_upsert", lambda cid, items: calls.append(("upsert", (cid, items)))
    )
    monkeypatch.setattr(
        whatsapp_cloud, "catalog_delete", lambda cid, rids: calls.append(("delete", (cid, rids)))
    )
    return calls


class TestTheRetailerId:
    def test_it_round_trips(self, db: Session) -> None:
        seller = make_seller(db)
        product = _published(db, seller)

        assert product_id_from_retailer(retailer_id(product)) == product.id

    def test_a_foreign_id_is_not_ours(self) -> None:
        assert product_id_from_retailer("SKU-42") is None
        assert product_id_from_retailer("soko_notanumber") is None


class TestSync:
    def test_a_published_priced_product_is_upserted(
        self, db: Session, catalogue_on: list[Any]
    ) -> None:
        seller = make_seller(db)
        product = _published(db, seller, price_kes=1800)

        assert catalog.sync_product(product) is True
        assert catalogue_on[0][0] == "upsert"
        _, (cid, items) = catalogue_on[0]
        assert cid == "cat_123"
        item = items[0]
        assert item["retailer_id"] == retailer_id(product)
        assert item["price"] == 180000  # minor units
        assert item["currency"] == "KES"
        assert item["availability"] == "in stock"

    def test_a_draft_is_removed(self, db: Session, catalogue_on: list[Any]) -> None:
        seller = make_seller(db)
        product = make_product(db, seller)  # DRAFT, no price

        catalog.sync_product(product)

        assert catalogue_on[0][0] == "delete"
        assert catalogue_on[0][1][1] == [retailer_id(product)]

    def test_a_sold_out_product_reads_as_out_of_stock(
        self, db: Session, catalogue_on: list[Any]
    ) -> None:
        seller = make_seller(db)
        product = _published(db, seller, stock=0)

        catalog.sync_product(product)

        assert catalogue_on[0][1][1][0]["availability"] == "out of stock"

    def test_no_catalogue_configured_does_nothing(self, db: Session) -> None:
        seller = make_seller(db)
        product = _published(db, seller)

        assert catalog.sync_product(product) is False


class TestPublishingSyncs:
    def test_publishing_enqueues_a_catalogue_sync(self, db: Session) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=500)

        publish_product(db, seller, product.id)

        queued = db.scalar(
            select(func.count(Job.id)).where(Job.kind == CATALOG_SYNC, Job.seller_id == seller.id)
        )
        assert queued == 1

    def test_the_job_syncs_the_product(
        self, db: Session, catalogue_on: list[Any], run_jobs: Any
    ) -> None:
        seller = make_seller(db)
        product = make_product(db, seller, price_kes=500)

        publish_product(db, seller, product.id)
        run_jobs()

        assert catalogue_on[0][0] == "upsert"


class TestTheCatalogueMessage:
    def test_it_sends_product_cards_when_a_catalogue_is_configured(
        self, db: Session, catalogue_on: list[Any]
    ) -> None:
        seller = make_seller(db, is_published=True)
        make_payment_method(db, seller)
        product = _published(db, seller)
        convo = get_conversation(db, BUYER_PHONE)
        convo.seller_id = seller.id

        outcome = handle(db, BUYER_PHONE, "catalogue")

        reply = outcome.replies[0]
        assert reply.product_list is not None
        header, sections = reply.product_list
        assert retailer_id(product) in sections[0][1]

    def test_it_falls_back_to_a_menu_without_a_catalogue(self, db: Session) -> None:
        seller = make_seller(db, is_published=True)
        _published(db, seller)
        convo = get_conversation(db, BUYER_PHONE)
        convo.seller_id = seller.id

        outcome = handle(db, BUYER_PHONE, "catalogue")

        assert all(r.product_list is None for r in outcome.replies)


class TestTheOrderComesBack:
    def test_an_order_message_opens_checkout(self, db: Session) -> None:
        seller = make_seller(db, is_published=True)
        make_payment_method(db, seller)
        product = _published(db, seller)

        outcome = handle_order(
            db, BUYER_PHONE, [{"product_retailer_id": retailer_id(product), "quantity": 2}]
        )

        convo = get_conversation(db, BUYER_PHONE)
        assert convo.state == ConversationState.CHECKOUT_NAME
        assert convo.seller_id == seller.id
        assert "name" in " ".join(r.body for r in outcome.replies).lower()

    def test_items_that_no_longer_exist_are_handled_gently(self, db: Session) -> None:
        make_seller(db, is_published=True)

        outcome = handle_order(
            db, BUYER_PHONE, [{"product_retailer_id": "soko_999999", "quantity": 1}]
        )

        assert "couldn't find" in " ".join(r.body for r in outcome.replies).lower()
