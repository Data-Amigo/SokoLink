"""
Who buys, derived from orders.

    Order.buyer_phone ──▶ one customer ──▶ orders · spend · last seen · segment

THERE IS NO CUSTOMER TABLE, DELIBERATELY. A buyer never creates an account —
that is the friction this whole product removes — so there is no moment at which
a customer record could be created, and nothing a seller could edit on one. What
a seller actually wants to know is *who has bought from me*, and orders already
say that completely.

Deriving it also means the answer cannot drift. A separate table would need
writing on every order, and the first missed write is a customer who bought
something and does not appear.

THE PHONE IS THE IDENTITY. It is the one field a buyer must give — it is the
M-Pesa line — and it is stable in a way a typed name is not: the same person is
"Akinyi", "akinyi o" and "Akinyi Otieno" across three orders. Grouping by name
would split them into three customers; grouping by phone does not.

SPEND COUNTS CONFIRMED MONEY ONLY. An order somebody claimed and never paid is
not spend, and showing it as such would overstate a seller's best customers to
their face.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Order, OrderStatus, Seller

#: Total confirmed spend at or above which a customer is "high value". A round
#: number chosen to be legible rather than derived — the point of the segment is
#: "who should I message first", and any threshold in this region answers it.
HIGH_VALUE_KES = 10_000


def _as_utc(value: datetime) -> datetime:
    """A timestamp that can be compared, whether or not it carries a timezone."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Customer:
    """One buyer, as their orders describe them."""

    phone: str
    name: str
    location: str | None
    orders: int
    spent_kes: int
    last_order_at: datetime
    has_unconfirmed: bool

    @property
    def initials(self) -> str:
        """Two letters for the avatar. Falls back to the phone when unnamed."""
        parts = [p for p in self.name.split() if p]
        if not parts:
            return self.phone[-2:]
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @property
    def segment(self) -> str:
        """
        The one label a seller sees against this name.

        Ordered by what should interrupt them: an unconfirmed payment is a task,
        a repeat buyer is an opportunity, everything else is context. Only one is
        shown, because a row wearing three badges communicates nothing.
        """
        if self.has_unconfirmed:
            return "Payment pending"
        if self.orders > 2:
            return "Repeat buyer"
        if self.orders == 2:
            return "Returning"
        return "New buyer"

    @property
    def display_phone(self) -> str:
        """
        The number as a Kenyan seller reads it: ``0712 345 678``.

        Stored international (``254712345678``) because that is what wa.me and
        Daraja need, but nobody in Nairobi says their number that way, and a
        seller scanning a list is matching it against their own memory.
        """
        digits = "".join(ch for ch in self.phone if ch.isdigit())
        local = "0" + digits[3:] if digits.startswith("254") and len(digits) == 12 else digits
        if len(local) == 10:
            return f"{local[:4]} {local[4:7]} {local[7:]}"
        return self.phone

    @property
    def whatsapp_url(self) -> str:
        """
        A wa.me link that opens this buyer's chat.

        THE WHOLE PAGE EXISTS FOR THIS LINK. wa.me wants digits with the country
        code and no plus, so a locally-typed 07xx is converted rather than sent
        as-is, which would open a chat with nobody.
        """
        digits = "".join(ch for ch in self.phone if ch.isdigit())
        if digits.startswith("0"):
            digits = "254" + digits[1:]
        return f"https://wa.me/{digits}"

    @property
    def is_repeat(self) -> bool:
        """Whether they have bought more than once."""
        return self.orders > 1

    @property
    def is_high_value(self) -> bool:
        """Whether they have spent enough to be worth messaging first."""
        return self.spent_kes >= HIGH_VALUE_KES


def list_customers(db: Session, seller: Seller, search: str | None = None) -> list[Customer]:
    """
    Everyone who has ordered from this shop, most recent first.

    Args:
        db: Session.
        seller: Whose shop.
        search: Free text matched against name, phone and delivery address.

    Returns:
        One :class:`Customer` per distinct phone number.

    Notes:
        THE NAME AND LOCATION COME FROM THE MOST RECENT ORDER, not the first.
        People move and correct their own spelling; the latest thing they told
        the seller is the thing worth showing.
    """
    query = select(Order).where(Order.seller_id == seller.id).order_by(Order.created_at.asc())

    if search and search.strip():
        # ILIKE over three columns. A shop has tens of customers, not millions —
        # a full-text index would be machinery bought with complexity we would
        # never earn back. `%` and `_` are escaped so a literal search stays literal.
        term = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{term}%"
        query = query.where(
            Order.buyer_name.ilike(pattern)
            | Order.buyer_phone.ilike(pattern)
            | Order.delivery_address.ilike(pattern)
        )

    # Oldest first above, so later orders overwrite earlier ones and each
    # customer ends up described by their most recent order.
    # str | int | bool | datetime | None, but spelling that out per key buys
    # nothing here — the dict is built and consumed twenty lines apart.
    grouped: dict[str, dict[str, Any]] = {}
    for order in db.scalars(query).all():
        entry = grouped.setdefault(
            order.buyer_phone,
            {"orders": 0, "spent": 0, "unconfirmed": False},
        )
        entry["orders"] += 1
        entry["name"] = order.buyer_name
        entry["location"] = order.delivery_address
        entry["last"] = order.created_at

        if order.status == OrderStatus.PAID.value:
            entry["spent"] += order.total_kes
        elif order.status == OrderStatus.AWAITING_CONFIRMATION.value:
            entry["unconfirmed"] = True

    customers = [
        Customer(
            phone=phone,
            name=data["name"],
            location=data.get("location"),
            orders=data["orders"],
            spent_kes=data["spent"],
            last_order_at=data["last"],
            has_unconfirmed=data["unconfirmed"],
        )
        for phone, data in grouped.items()
    ]

    customers.sort(key=lambda c: c.last_order_at, reverse=True)
    return customers


#: The segment filters offered on the page, and what each one means. Kept beside
#: the definitions they filter on so a new segment cannot be added to the UI
#: without deciding here what it actually selects.
SEGMENTS: dict[str, tuple[str, Callable[[Customer], bool]]] = {
    "repeat": ("Repeat buyers", lambda c: c.is_repeat),
    "new": ("New buyers", lambda c: not c.is_repeat),
    "high": ("High value", lambda c: c.is_high_value),
    "pending": ("Payment pending", lambda c: c.has_unconfirmed),
}


def filter_segment(customers: list[Customer], segment: str | None) -> list[Customer]:
    """
    Narrow a list to one segment.

    Args:
        customers: From :func:`list_customers`.
        segment: A key of :data:`SEGMENTS`. Anything else returns everyone,
            because a mistyped URL should show the page, not an error.

    Returns:
        The matching customers, in the order they arrived.

    Notes:
        THIS IS WHY THE SEGMENT COUNTS ARE CLICKABLE. A count a seller cannot
        act on is decoration — "7 repeat buyers" is only useful next to the
        seven names and seven message buttons.
    """
    if segment not in SEGMENTS:
        return customers
    matches = SEGMENTS[segment][1]
    return [c for c in customers if matches(c)]


def customer_summary(db: Session, seller: Seller, customers: list[Customer]) -> dict[str, int]:
    """
    The tiles above the list.

    Args:
        db: Session, for the order count the "new this month" tile cites.
        seller: Whose shop.
        customers: Already computed, so the page does not group twice.

    Returns:
        ``total``, ``new_this_month``, ``repeat``, ``reachable``,
        ``return_rate`` (whole percent), ``reachable_percent``, ``orders``,
        ``high_value`` and ``payment_pending``.

    Notes:
        EVERY CUSTOMER IS WHATSAPP-REACHABLE, because the phone they checked out
        with is the only way they exist here. The tile is kept because it will
        stop being 100% the moment a seller records a walk-in by hand — and a
        number that is always 100% is worth showing precisely once it can drop.
    """
    month_start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    total = len(customers)
    # created_at is timezone-aware in Postgres, but a naive value can still
    # reach here from a fixture that built the row in Python. Treating a naive
    # timestamp as UTC beats raising on a comparison.
    new_this_month = sum(
        1 for c in customers if c.orders == 1 and _as_utc(c.last_order_at) >= month_start
    )
    repeat = sum(1 for c in customers if c.is_repeat)

    orders = db.scalar(
        select(func.count(Order.id)).where(
            Order.seller_id == seller.id, Order.created_at >= month_start
        )
    )

    return {
        "total": total,
        "new_this_month": new_this_month,
        "repeat": repeat,
        "reachable": total,
        "return_rate": round(repeat / total * 100) if total else 0,
        "reachable_percent": 100 if total else 0,
        "orders": int(orders or 0),
        "high_value": sum(1 for c in customers if c.is_high_value),
        "payment_pending": sum(1 for c in customers if c.has_unconfirmed),
    }
