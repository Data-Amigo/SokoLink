"""
Two gaps found in live testing, and the guards that keep them fixed.

1. "open another shop" was read as SELLER_OPEN and opened the seller's EXISTING
   shop. Multi-shop now supports several shops per number, so that phrase STARTS
   a new shop instead — and never opens the one they already have.
2. A buyer pasted a whole M-Pesa confirmation SMS and was told it "doesn't look
   like a code". The reference is now extracted from anywhere in the message.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ConversationState, ProductStatus
from app.services.bot import get_conversation, handle
from app.services.bot.buying import _mpesa_code
from app.services.bot.common import find_account_by_phone
from tests.factories import make_payment_method, make_product, make_seller

PHONE = "254712345678"


def _onboard_owner(db: Session) -> None:
    """Create a real account-backed owner via the chat, so multi-shop applies."""
    handle(db, PHONE, "sell")
    handle(db, PHONE, "Book Nook")
    handle(db, PHONE, "skip")  # defer payment; account + first shop now exist


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
    def test_open_another_shop_starts_a_new_shop(self, db: Session) -> None:
        _onboard_owner(db)

        said = _say(db, "What if I want to open another shop for computers")

        # Starts creating a new shop — it does NOT open the existing one.
        assert "another shop" in said.lower()
        assert "open for business" not in said.lower()
        assert get_conversation(db, PHONE).state == ConversationState.NAMING

    def test_add_a_shop_starts_a_new_shop(self, db: Session) -> None:
        _onboard_owner(db)

        said = _say(db, "I want to add a shop")

        assert "another shop" in said.lower()
        assert get_conversation(db, PHONE).state == ConversationState.NAMING

    def test_naming_it_creates_a_second_shop(self, db: Session) -> None:
        _onboard_owner(db)
        _say(db, "new shop")

        _say(db, "Computer World")

        account = find_account_by_phone(db, PHONE)
        assert account is not None
        names = {s.display_name for s in account.sellers}
        assert names == {"Book Nook", "Computer World"}

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
