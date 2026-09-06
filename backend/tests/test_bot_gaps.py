"""
Two gaps found in live testing, and the guards that keep them fixed.

1. "open another shop" was read as SELLER_OPEN and opened the seller's EXISTING
   shop. One WhatsApp number runs one shop today, so the request now gets an
   honest answer instead of the model's misread.
2. A buyer pasted a whole M-Pesa confirmation SMS and was told it "doesn't look
   like a code". The reference is now extracted from anywhere in the message.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ProductStatus
from app.services.bot import handle
from app.services.bot.buying import _mpesa_code
from tests.factories import make_payment_method, make_product, make_seller

PHONE = "254712345678"


def _openable_seller(db: Session):
    """A closed shop that COULD be opened — live item + a way to be paid."""
    seller = make_seller(db, whatsapp_number=PHONE, is_published=False)
    make_product(
        db,
        seller,
        title="Laptop",
        status=ProductStatus.PUBLISHED.value,
        price_kes=45000,
        stock=3,
        platform_post_id="710000000000000123",
    )
    make_payment_method(db, seller)
    return seller


def _say(db: Session, text: str) -> str:
    return "\n".join(r.body for r in handle(db, PHONE, text).replies)


class TestAnotherShop:
    def test_open_another_shop_does_not_open_the_existing_one(self, db: Session) -> None:
        seller = _openable_seller(db)

        said = _say(db, "What if I want to open another shop for computers")

        assert "one shop" in said.lower()
        assert "open for business" not in said.lower()
        assert seller.is_published is False

    def test_add_a_shop_is_understood(self, db: Session) -> None:
        seller = _openable_seller(db)

        said = _say(db, "I want to add a shop")

        assert "one shop" in said.lower()
        assert seller.is_published is False

    def test_plain_open_still_opens_the_shop(self, db: Session) -> None:
        # Regression: the guard must not swallow the real open command.
        seller = _openable_seller(db)

        said = _say(db, "open")

        assert "open for business" in said.lower()
        assert seller.is_published is True


class TestMpesaCodeExtraction:
    def test_extracts_the_code_from_a_pasted_sms(self) -> None:
        sms = (
            "UI2G24W3HA Confirmed. Ksh20.00 sent to Lipa na KCB for account "
            "7568710 on 2/9/26 at 7:49 PM New M-PESA balance is Ksh0.00."
        )
        assert _mpesa_code(sms) == "UI2G24W3HA"

    def test_a_bare_code_is_accepted_and_upper_cased(self) -> None:
        assert _mpesa_code("slk7xa2b9c") == "SLK7XA2B9C"

    def test_plain_words_are_not_a_code(self) -> None:
        assert _mpesa_code("i have paid already") is None

    def test_an_account_number_alone_is_not_a_code(self) -> None:
        assert _mpesa_code("paid to 7568710") is None
