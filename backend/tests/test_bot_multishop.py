"""
Multi-shop: one WhatsApp number can run several shops, and switch/close/delete
between them. The active shop is the conversation's ``managing_shop_id``; every
owner action resolves to it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Seller
from app.services.bot import get_conversation, handle
from app.services.bot.common import find_account_by_phone

PHONE = "254712345678"


def _say(db: Session, text: str) -> str:
    return "\n".join(r.body for r in handle(db, PHONE, text).replies)


def _two_shops(db: Session) -> None:
    """Onboard 'Book Nook', then add 'Computer World' (which becomes active)."""
    handle(db, PHONE, "sell")
    handle(db, PHONE, "Book Nook")
    handle(db, PHONE, "skip")
    handle(db, PHONE, "new shop")
    handle(db, PHONE, "Computer World")
    handle(db, PHONE, "skip")


def _shop(db: Session, name: str) -> Seller:
    account = find_account_by_phone(db, PHONE)
    assert account is not None
    return next(s for s in account.sellers if s.display_name == name)


class TestMultiShop:
    def test_new_shop_becomes_the_active_shop(self, db: Session) -> None:
        _two_shops(db)
        assert get_conversation(db, PHONE).managing_shop_id == _shop(db, "Computer World").id

    def test_my_shops_lists_both_and_marks_the_active_one(self, db: Session) -> None:
        _two_shops(db)
        said = _say(db, "my shops")
        assert "Book Nook" in said
        assert "Computer World" in said
        assert "you're here" in said.lower()

    def test_switch_changes_the_active_shop(self, db: Session) -> None:
        _two_shops(db)
        book_nook = _shop(db, "Book Nook")

        said = _say(db, f"switch:{book_nook.id}")

        assert "book nook" in said.lower()
        assert get_conversation(db, PHONE).managing_shop_id == book_nook.id

    def test_close_unpublishes_the_active_shop(self, db: Session) -> None:
        _two_shops(db)
        computer_world = _shop(db, "Computer World")
        computer_world.is_published = True
        db.flush()

        said = _say(db, "close")

        assert computer_world.is_published is False
        assert "closed" in said.lower()

    def test_delete_needs_confirmation_then_archives_and_switches(self, db: Session) -> None:
        _two_shops(db)
        computer_world = _shop(db, "Computer World")

        _say(db, "delete")
        assert computer_world.archived_at is None  # asked, not done

        _say(db, "delete Computer World")

        # Re-read after the delete: the assert on line above narrowed
        # archived_at to None, and mypy can't see the delete mutated it.
        computer_world = _shop(db, "Computer World")
        assert computer_world.archived_at is not None
        # active falls back to the remaining shop
        assert get_conversation(db, PHONE).managing_shop_id == _shop(db, "Book Nook").id

    def test_delete_can_be_cancelled(self, db: Session) -> None:
        _two_shops(db)
        _say(db, "delete")

        said = _say(db, "cancel")

        assert _shop(db, "Computer World").archived_at is None
        assert "kept" in said.lower()

    def test_a_deleted_shop_is_no_longer_listed(self, db: Session) -> None:
        _two_shops(db)
        _say(db, "delete")
        _say(db, "delete Computer World")

        said = _say(db, "my shops")

        assert "Computer World" not in said
        assert "Book Nook" in said
