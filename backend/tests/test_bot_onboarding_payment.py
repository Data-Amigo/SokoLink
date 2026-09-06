"""
Onboarding now captures how a seller gets paid, right after they name the shop.

WHY IT MOVED INTO ONBOARDING. Payment setup used to surface only at the open
gate — a seller built a catalogue and then discovered they had no way to be
paid. Naming now leads straight into "how do you take M-Pesa?", with *skip* for
anyone who would rather add stock first. No secrets are asked in chat; this is
the pay number only.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ConversationState
from app.services.bot import get_conversation, handle
from app.services.bot.common import find_seller_by_phone
from app.services.orders import get_payment_method

NEW = "254799000111"


def _say(db: Session, text: str, phone: str = NEW) -> str:
    return "\n".join(r.body for r in handle(db, phone, text).replies)


def _name_a_shop(db: Session, phone: str = NEW) -> None:
    _say(db, "sell", phone)
    _say(db, "Mama Watoto Collections", phone)


class TestOnboardingPayment:
    def test_naming_leads_into_payment_setup(self, db: Session) -> None:
        _say(db, "sell")
        said = _say(db, "Mama Watoto Collections")

        assert "m-pesa" in said.lower()
        assert get_conversation(db, NEW).state == ConversationState.PAY_KIND

    def test_setting_pochi_saves_and_points_to_first_item(self, db: Session) -> None:
        _name_a_shop(db)

        _say(db, "pochi")
        said = _say(db, "0700111222")

        seller = find_seller_by_phone(db, NEW)
        assert seller is not None
        assert get_payment_method(db, seller) is not None
        assert "forward" in said.lower()

    def test_skip_defers_payment_but_still_onboards(self, db: Session) -> None:
        _name_a_shop(db)

        said = _say(db, "skip")

        seller = find_seller_by_phone(db, NEW)
        assert seller is not None
        assert get_payment_method(db, seller) is None
        assert "forward" in said.lower()
        assert get_conversation(db, NEW).state == ConversationState.NEW
