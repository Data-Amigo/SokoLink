"""
What a seller has actually earned — the money tracker.

    Order(paid) ──┬──▶ sales this month
                  ├──▶ paid orders
                  └──▶ average order

    Order(awaiting_confirmation) ──▶ waiting on you

WHY THIS REPLACED THE SOCIAL ANALYTICS PAGE. That page showed views, followers
and post counts, which belonged to the content-first direction that was parked.
A seller running a shop asks one question when they open the app: **did I make
any money, and is anyone waiting on me?**

WHAT THIS DELIBERATELY DOES NOT SHOW: a balance.

The obvious design — the one in the mockup — has an "available balance" tile.
We cannot honestly show one. Buyers pay the seller's Pochi or till **directly**;
we are never in the money path and hold nothing. Any balance here would be a
number we inferred, and the first time it disagreed with the seller's own M-Pesa
they would stop trusting every other number on the page.

So the tile in that position is **awaiting confirmation** — money a buyer says
they have sent that the seller has not yet verified. It is the number that is
actually actionable, and it is one we can stand behind.

EVERYTHING IS SCOPED TO ONE SELLER, and this page is seller-only. A buyer sees
their own order and receipt, never this.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Order, OrderStatus, Seller

#: How many recent orders the tracker lists before "View all".
RECENT_LIMIT = 8


def _month_start(now: datetime | None = None) -> datetime:
    """
    Midnight on the first of the current month, UTC.

    A calendar month rather than a rolling 30 days, because a seller comparing
    against their own sense of "this month" means the calendar one. A rolling
    window produces a number they cannot reconcile with anything.
    """
    moment = now or datetime.now(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def money_summary(db: Session, seller: Seller, now: datetime | None = None) -> dict[str, int]:
    """
    The four numbers at the top of the tracker.

    Args:
        db: Session.
        seller: Whose shop.
        now: Override for testing; defaults to the current time.

    Returns:
        ``sales_this_month`` — confirmed money, in whole KES, since the 1st.
        ``paid_orders`` — how many orders that was.
        ``average_order`` — the mean, rounded down; zero when nothing sold.
        ``awaiting_kes`` — claimed but unconfirmed, the seller's actual to-do.

    Notes:
        ``awaiting_kes`` counts orders in ``AWAITING_CONFIRMATION`` regardless of
        month. Money somebody says they sent in June is still waiting in July,
        and hiding it behind a date filter would let it sit forever.
    """
    since = _month_start(now)

    paid = db.execute(
        select(func.coalesce(func.sum(Order.total_kes), 0), func.count(Order.id)).where(
            Order.seller_id == seller.id,
            Order.status == OrderStatus.PAID.value,
            Order.paid_at >= since,
        )
    ).one()
    sales_this_month, paid_orders = int(paid[0]), int(paid[1])

    awaiting = db.scalar(
        select(func.coalesce(func.sum(Order.total_kes), 0)).where(
            Order.seller_id == seller.id,
            Order.status == OrderStatus.AWAITING_CONFIRMATION.value,
        )
    )

    return {
        "sales_this_month": sales_this_month,
        "paid_orders": paid_orders,
        # Integer division on purpose. Money is integer KES everywhere in this
        # codebase, and an average is a summary rather than an amount anyone is
        # charged — a rounded shilling here misleads nobody.
        "average_order": sales_this_month // paid_orders if paid_orders else 0,
        "awaiting_kes": int(awaiting or 0),
    }


def recent_transactions(db: Session, seller: Seller, limit: int = RECENT_LIMIT) -> list[Order]:
    """
    The seller's most recent orders, whatever their state.

    Includes unpaid and awaiting ones deliberately. A list of only successes
    reads as a bank statement and hides the thing the seller has to act on —
    which is precisely the order somebody is waiting to have confirmed.

    Notes:
        ORDERED BY id AS WELL AS created_at, and the tiebreak is not decoration.
        Both timestamps come from ``now()``, which in Postgres is the start of
        the TRANSACTION — so two orders placed in one request are identical to
        the microsecond, and a sort on the timestamp alone leaves their order
        to the planner. A seller refreshing the page watched two same-second
        orders swap places.

        It surfaced as a test that failed roughly one run in two and passed on
        retry, which is the shape of a bug that gets dismissed as flakiness. The
        id is monotonic, so this makes "newest first" mean something.
    """
    return list(
        db.scalars(
            select(Order)
            .where(Order.seller_id == seller.id)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
        ).all()
    )
