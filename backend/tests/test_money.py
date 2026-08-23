"""
The seller's money tracker.

Replaced the social analytics page when the product became store-first. A
seller opens the app asking one question: did I make any money, and is anyone
waiting on me?

The thing being defended hardest is what this page **does not** claim. We are
never in the money path — buyers pay the seller's Pochi or till directly — so
there is no balance we can honestly show. Every number here has to be one we
can stand behind, because the first one that disagrees with the seller's own
M-Pesa costs us every other number on the page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models import Seller
from app.services.cart import add_item, get_or_create_cart
from app.services.money import money_summary, recent_transactions
from app.services.orders import claim_payment, confirm_payment, place_order
from tests.factories import make_payment_method, make_product, make_seller


def shop(db: Session, **overrides: Any) -> Seller:
    seller = make_seller(db, **overrides)
    seller.is_published = True
    db.flush()
    make_payment_method(db, seller)
    return seller


def sale(db: Session, seller: Seller, price_kes: int, *, post_id: str) -> Any:
    """A placed order for one item at ``price_kes``."""
    product = make_product(
        db, seller, price_kes=price_kes, stock=50, status="published", platform_post_id=post_id
    )
    cart = get_or_create_cart(db, None, seller)
    add_item(db, cart, product.id)
    return place_order(db, cart, buyer_name="Amina", buyer_phone="254712000111")


def settle(db: Session, order: Any, code: str = "SLK7XA2B9C") -> None:
    """Take an order all the way to paid, the way the seller would."""
    claim_payment(db, order, code)
    confirm_payment(db, order)


class TestTheFourNumbers:
    def test_an_empty_shop_shows_zeroes_rather_than_nothing(self, db: Session) -> None:
        """A seller with no sales still needs a page that renders."""
        seller = shop(db)

        assert money_summary(db, seller) == {
            "sales_this_month": 0,
            "paid_orders": 0,
            "average_order": 0,
            "awaiting_kes": 0,
        }

    def test_only_confirmed_money_counts_as_sales(self, db: Session) -> None:
        """
        An order placed is not money. On the manual path a buyer can claim to
        have paid and be wrong — counting that as revenue would overstate a
        seller's earnings to their face.
        """
        seller = shop(db)
        settle(db, sale(db, seller, 1500, post_id="a"))
        sale(db, seller, 9999, post_id="b")  # left pending

        summary = money_summary(db, seller)

        assert summary["sales_this_month"] == 1500
        assert summary["paid_orders"] == 1

    def test_a_claim_is_waiting_not_earned(self, db: Session) -> None:
        """
        THE DISTINCTION THE WHOLE MANUAL PATH RESTS ON, shown as a number. Money
        somebody says they sent sits in its own tile until the seller confirms.
        """
        seller = shop(db)
        order = sale(db, seller, 2000, post_id="a")
        claim_payment(db, order, "SLK7XA2B9C")

        summary = money_summary(db, seller)

        assert summary["awaiting_kes"] == 2000
        assert summary["sales_this_month"] == 0

    def test_confirming_moves_it_from_waiting_to_sales(self, db: Session) -> None:
        seller = shop(db)
        order = sale(db, seller, 2000, post_id="a")
        claim_payment(db, order, "SLK7XA2B9C")
        confirm_payment(db, order)

        summary = money_summary(db, seller)

        assert summary["awaiting_kes"] == 0
        assert summary["sales_this_month"] == 2000

    def test_the_average_is_the_mean_of_what_was_paid(self, db: Session) -> None:
        seller = shop(db)
        settle(db, sale(db, seller, 1000, post_id="a"), code="CODE1")
        settle(db, sale(db, seller, 2000, post_id="b"), code="CODE2")

        summary = money_summary(db, seller)

        assert summary["paid_orders"] == 2
        assert summary["average_order"] == 1500

    def test_last_months_sales_do_not_count_as_this_months(self, db: Session) -> None:
        """ "Sales this month" has to mean the calendar month a seller means."""
        seller = shop(db)
        order = sale(db, seller, 5000, post_id="a")
        settle(db, order)
        # Backdate the payment into the previous month.
        order.paid_at = datetime.now(UTC).replace(day=1) - timedelta(days=2)
        db.flush()

        assert money_summary(db, seller)["sales_this_month"] == 0

    def test_waiting_money_is_never_hidden_by_the_month_filter(self, db: Session) -> None:
        """
        Money somebody claimed in June is still waiting in July. Filtering it by
        date would let an unconfirmed order sit forever, unseen.
        """
        seller = shop(db)
        order = sale(db, seller, 3000, post_id="a")
        claim_payment(db, order, "SLK7XA2B9C")
        order.created_at = datetime.now(UTC) - timedelta(days=90)
        db.flush()

        assert money_summary(db, seller)["awaiting_kes"] == 3000

    def test_everything_is_scoped_to_one_seller(self, db: Session) -> None:
        mine = shop(db, slug="mine")
        theirs = shop(db, slug="theirs", display_name="Theirs")
        settle(db, sale(db, theirs, 7000, post_id="a"))

        assert money_summary(db, mine)["sales_this_month"] == 0
        assert money_summary(db, theirs)["sales_this_month"] == 7000


class TestRecentTransactions:
    def test_unpaid_orders_are_listed_too(self, db: Session) -> None:
        """
        A list of only successes reads as a bank statement and hides the thing
        the seller has to act on.
        """
        seller = shop(db)
        settle(db, sale(db, seller, 1000, post_id="a"))
        pending = sale(db, seller, 2000, post_id="b")

        references = [o.reference for o in recent_transactions(db, seller)]

        assert pending.reference in references

    def test_newest_first(self, db: Session) -> None:
        seller = shop(db)
        first = sale(db, seller, 1000, post_id="a")
        second = sale(db, seller, 2000, post_id="b")

        rows = recent_transactions(db, seller)

        assert rows[0].reference == second.reference
        assert rows[1].reference == first.reference

    def test_it_is_scoped_to_one_seller(self, db: Session) -> None:
        mine = shop(db, slug="mine")
        theirs = shop(db, slug="theirs", display_name="Theirs")
        sale(db, theirs, 1000, post_id="a")

        assert recent_transactions(db, mine) == []

    def test_the_limit_is_respected(self, db: Session) -> None:
        seller = shop(db)
        for n in range(4):
            sale(db, seller, 100 * (n + 1), post_id=f"p{n}")

        assert len(recent_transactions(db, seller, limit=2)) == 2


class TestThePage:
    def test_it_is_behind_the_login_wall(self, client: Any, db: Session) -> None:
        """Seller-only. A buyer sees their own order and receipt, never this."""
        response = client.get("/analytics", follow_redirects=False)
        assert response.status_code in (302, 303, 307)
        assert "/login" in response.headers["location"]

    def test_it_never_claims_to_know_a_balance(self, client: Any, db: Session) -> None:
        """
        We hold no money and cannot see the seller's M-Pesa. A balance here would
        be inferred, and the first time it disagreed with their phone they would
        stop trusting every other number on the page.
        """
        from app.services.accounts import create_account

        create_account(
            db, email="s@example.com", password="correct-horse-battery", shop_name="Nairobi Thrift"
        )
        db.flush()
        client.post("/login", data={"email": "s@example.com", "password": "correct-horse-battery"})

        body = client.get("/analytics").text.lower()

        assert "available balance" not in body
        assert "cannot see your balance" in body
