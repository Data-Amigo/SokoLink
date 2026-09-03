"""
Telling people money moved.

    order becomes paid ──▶ buyer gets a receipt
                      └──▶ seller learns they have a sale

WHY THIS IS A SERVICE AND NOT A FEW LINES IN THE CALLBACK. Three rules apply to
every message here and none of them are obvious at a call site: consent, the
24-hour window, and the fact that a failed notification must never undo a
payment. Putting them in one place is what stops the second caller getting one
of them wrong.

NOTHING HERE DECIDES ANYTHING. It reads an order that is already paid and says
so. It never marks, confirms, or changes a payment — the callback and the
seller do that, and a notifier that could alter state would be a second source
of truth about money.

A FAILURE HERE IS NEVER FATAL. The money has arrived and the order is recorded;
if the message does not send, the seller still sees the sale in their workspace
and the buyer still has their M-Pesa confirmation. Raising would fail a webhook
that has already done the important part, and Daraja would redeliver it.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import Order, Seller
from app.services.messaging import MessagingError, Messenger, get_messenger

logger = logging.getLogger(__name__)


def _money(amount: int) -> str:
    """Kenyan shillings, grouped — money here is integer KES."""
    return f"KSh {amount:,}"


def buyer_receipt(order: Order, seller: Seller) -> str:
    """
    What the buyer reads when their payment lands.

    Names the shop, the amount and the reference, because this message is the
    thing they will scroll back to when something goes wrong — and a receipt
    that does not identify the order it belongs to is not a receipt.
    """
    lines = [f"• {item.title} × {item.quantity}" for item in order.items]
    return (
        f"Payment received. ✅\n\n"
        f"*{seller.display_name}*\n"
        f"Order *{order.reference}*\n\n" + "\n".join(lines) + f"\n\n"
        f"Paid: *{_money(order.total_kes)}*\n\n"
        f"{seller.display_name} will be in touch about delivery."
    )


def order_placed_alert(order: Order) -> str:
    """
    What the seller reads the moment an order is placed — before any money.

    Notes:
        SEPARATE FROM ``seller_alert``, which says "paid" and must keep saying
        it. This one is the opposite: nothing has arrived yet, and telling a
        seller they have been paid when they have not is the one lie that would
        cost them real money — they would hand over the goods.

        IT GOES OUT AT PLACEMENT, not at payment. A seller who only hears about
        an order once it is settled cannot set anything aside, and the buyer is
        standing in a shop somewhere waiting to be told it is coming.
    """
    lines = [f"• {item.title} × {item.quantity}" for item in order.items]
    where = f"\n{order.delivery_address}" if order.delivery_address else ""
    collecting = "" if order.delivery_address else "\n_Collecting in person._"
    return (
        f"New order. 🛒\n\n"
        f"*{order.reference}*\n\n" + "\n".join(lines) + f"\n\n"
        f"Total: *{_money(order.total_kes)}*\n\n"
        f"{order.buyer_name}\n"
        f"{order.buyer_phone}{where}{collecting}\n\n"
        f"_They are paying now. I'll tell you when they send the code._"
    )


def payment_claimed_alert(order: Order, code: str) -> str:
    """
    What the seller reads when a buyer says they have paid.

    Notes:
        THE WORDING IS CAREFUL AND HAS TO BE. A code is text the buyer typed; it
        looks identical whether it came off a real M-Pesa message or was
        invented. So this says what the buyer SAYS, names the amount to check
        against, and asks the seller to look at their own phone. Anything
        shorter reads as "you have been paid", which is not ours to assert.
    """
    return (
        f"{order.buyer_name} says they've paid.\n\n"
        f"*{order.reference}* — *{_money(order.total_kes)}*\n"
        f"M-Pesa code: *{code}*\n\n"
        f"_Check your M-Pesa, then confirm it below._"
    )


def seller_alert(order: Order) -> str:
    """
    What the seller reads when they make a sale.

    THE BUYER'S NUMBER IS IN IT ON PURPOSE. A seller's next action is almost
    always to message the person about delivery, and making them open a browser
    to find a phone number they were just told about is the friction this whole
    product exists to remove.
    """
    where = f"\n{order.delivery_address}" if order.delivery_address else ""
    return (
        f"You have a sale. 🎉\n\n"
        f"Order *{order.reference}*\n"
        f"*{_money(order.total_kes)}* — paid\n\n"
        f"{order.buyer_name}\n"
        f"{order.buyer_phone}{where}\n\n"
        f"_Arrange delivery with them when you can._"
    )


def notify_paid(db: Session, order: Order, messenger: Messenger | None = None) -> list[str]:
    """
    Tell both sides that an order has been paid.

    Args:
        db: Session. Nothing here writes, but the order must be attached.
        order: A PAID order. Called after the callback has committed.
        messenger: The seam. A fake in tests; nothing leaves the process there.

    Returns:
        Which parties were actually messaged — ``["buyer", "seller"]`` or a
        subset. The caller logs it; nothing branches on it.

    Notes:
        THE BUYER IS MESSAGED ONLY WITH CONSENT. ``whatsapp_opt_in`` is asked
        for at checkout and is the buyer's answer, not our convenience. A
        webview does not know who is looking at it — a link opened from a status
        is just a browser tab — so the phone we hold was typed for M-Pesa, and
        using it for anything else without the tick would be helping ourselves
        to it.

        THE SELLER MAY BE UNREACHABLE, and that is expected rather than an
        error. Meta delivers free-form messages only inside the 24-hour window
        a person opens by messaging us. A buyer who just paid is comfortably
        inside it; a seller who has been quiet for a week is not, and their
        alert will fail until an approved template exists. They still see the
        sale in the workspace, which is why this is a log line and not a raise.
    """
    sender = messenger or get_messenger()
    seller = order.seller
    reached: list[str] = []

    if order.whatsapp_opt_in and order.buyer_phone:
        try:
            sender.send(order.buyer_phone, buyer_receipt(order, seller))
            reached.append("buyer")
        except MessagingError as exc:
            logger.warning("receipt not delivered for %s: %s", order.reference, exc)

    if seller.whatsapp_number:
        try:
            sender.send(seller.whatsapp_number, seller_alert(order))
            reached.append("seller")
        except MessagingError as exc:
            # Almost always a closed 24-hour window. Expected, not broken.
            logger.warning("sale alert not delivered for %s: %s", order.reference, exc)

    return reached
