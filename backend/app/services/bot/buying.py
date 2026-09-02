"""
The buyer's side of the thread: browse, cart, checkout, pay, ask.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ConversationState,
    Order,
    Product,
    Seller,
    WaConversation,
)
from app.schemas.conversation import Intent
from app.services.bot.common import _basket, _tell_seller
from app.services.bot.presentation import (
    _add_to_basket,
    _ask_for,
    _ask_variant,
    _found,
    _menu,
    _show_cart,
    _start_checkout,
)
from app.services.bot.reading import _understand
from app.services.bot.replies import PAGE_SIZE, Outcome, Reply, _money
from app.services.daraja import get_stk_engine
from app.services.notifications import (
    order_placed_alert,
    payment_claimed_alert,
)
from app.services.orders import (
    OrderError,
    claim_payment,
    get_payment_method,
    place_order,
)
from app.services.payments import MPESA_CALLBACK_PATH, PaymentError, start_stk_payment
from app.services.questions import ask
from app.services.storefront import get_public_products


def _place(
    db: Session,
    seller: Seller,
    convo: WaConversation,
    phone: str,
    *,
    name: str,
    address: str | None,
) -> Outcome:
    """
    Turn the basket into an order, tell the buyer how to pay, tell the seller
    it happened.

    Notes:
        THE SELLER IS TOLD AT PLACEMENT, not at payment. Until this, the only
        thing that notified anybody was the Daraja callback — so a seller on
        Pochi, which is most of them, found out about orders by happening to
        type `orders`. Their phone never rang.
    """
    cart = _basket(db, convo, seller)
    if not cart.items:
        return Outcome(_show_cart(db, seller, convo))

    try:
        order = place_order(
            db,
            cart=cart,
            buyer_name=name,
            buyer_phone=phone,
            delivery_address=address,
            # They are talking to us on WhatsApp; the receipt goes to the same
            # thread. Consent is the act of ordering here, not a checkbox.
            whatsapp_opt_in=True,
        )
    except OrderError as exc:
        convo.state = ConversationState.CART
        convo.context = {}
        return Outcome([Reply(str(exc), buttons=[("menu", "Keep shopping")])])

    db.flush()

    convo.state = ConversationState.PAYING
    convo.order_reference = order.reference
    convo.context = {}

    return Outcome(
        _ask_for_payment(db, seller, order),
        notify=_tell_seller(seller, Reply(order_placed_alert(order))),
    )


def _ask_for_payment(db: Session, seller: Seller, order: Order) -> list[Reply]:
    """
    Ask for the money the way this seller can actually receive it.

    Args:
        db: Session.
        seller: Whose shop.
        order: The order just placed.

    Returns:
        A prompt on the buyer's handset when the seller is STK-capable, and the
        manual instruction otherwise.

    Notes:
        TWO PATHS, AND THE MANUAL ONE IS PERMANENT. Daraja's STK works with
        Paybill and Buy Goods shortcodes only, so a Pochi seller can never
        receive a push — and a large share of Kenyan micro-sellers run on
        Pochi. This is not a fallback waiting to be removed.

        A FAILED PUSH IS NOT A FAILED ORDER. If Daraja refuses — expired
        credentials, a shortcode that is not live, Safaricom having a bad
        afternoon — the buyer is told the number to pay by hand. The order
        exists either way, and abandoning a sale because an API was unavailable
        would be the worst possible response to it.

        EVEN AFTER A PUSH, THE CODE PATH STAYS OPEN. The conversation remains
        in PAYING, so a buyer whose prompt never arrived can still type the
        code from their M-Pesa message.
    """
    method = get_payment_method(db, seller)
    to_name = f" ({order.paid_to_name})" if order.paid_to_name else ""

    manual = Reply(
        f"*Order {order.reference}*\n"
        f"Total: *{_money(order.total_kes)}*\n\n"
        f"Send the money to *{order.paid_to_number}*{to_name} on M-Pesa, "
        f"then reply here with the M-Pesa code.\n\n"
        f"_{seller.display_name} confirms it once the money shows up._"
    )

    if method is None or not method.can_stk:
        return [manual]

    try:
        start_stk_payment(
            db,
            order,
            method,
            get_stk_engine(),
            f"{settings.app_base_url}{MPESA_CALLBACK_PATH}",
        )
    except PaymentError:
        # Say nothing about the failure: "our payment API is down" is our
        # problem described in our words, and the buyer can still pay.
        return [manual]

    return [
        Reply(
            f"*Order {order.reference}*\n"
            f"Total: *{_money(order.total_kes)}*\n\n"
            f"Check your phone — there's an M-Pesa prompt waiting. "
            f"Enter your PIN to pay {seller.display_name}.\n\n"
            f"_Didn't get it? Send the money to {order.paid_to_number}{to_name} "
            f"and reply here with the code._"
        )
    ]


def _claim(db: Session, convo: WaConversation, text: str) -> Outcome:
    """
    Record the code the buyer says they paid with.

    A CODE IS A CLAIM, NOT A PAYMENT. Nothing here marks the order paid — only
    the seller does that, from the workspace, after checking their own M-Pesa
    messages. Saying "payment received" at this point would be a lie the buyer
    would believe.
    """
    code = text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,15}", code):
        return Outcome(
            [
                Reply(
                    "That doesn't look like an M-Pesa code. It's the reference in "
                    "the M-Pesa message, like _SLK7XA2B9C_.\n\n"
                    "Send it here once you've paid."
                )
            ]
        )

    order = db.scalar(select(Order).where(Order.reference == convo.order_reference))
    if order is None:
        convo.state = ConversationState.BROWSING
        convo.order_reference = None
        return Outcome([Reply("I've lost track of that order. Send *menu* to start again.")])

    claim_payment(db, order, code=code)

    # THE SELLER IS TOLD, WITH THE CONFIRM BUTTON ON IT. The reply below has
    # always promised the buyer "you'll get a message here when they do" — a
    # promise nothing kept, because nothing told the seller there was anything
    # to confirm. They had to guess to type `orders`.
    alert = Reply(
        payment_claimed_alert(order, code),
        buttons=[(f"confirm:{order.reference}", "Confirm paid"[:20])],
    )

    return Outcome(
        [
            Reply(
                f"Got it — *{code}* recorded against order *{order.reference}*.\n\n"
                "The seller will confirm once the money shows in their M-Pesa. "
                "You'll get a message here when they do."
            )
        ],
        notify=_tell_seller(order.seller, alert),
    )


def _about_this_item(convo: WaConversation, db: Session, seller: Seller) -> list[Reply] | None:
    """
    Answer a question about the item on screen, from the item itself.

    Returns:
        The product's own facts, or None when there is nothing on screen to be
        asking about — in which case the caller hands the question to the shop.

    Notes:
        EVERY WORD OF THIS COMES OUT OF THE ROW. The title, the price, the
        description the seller wrote, the sizes they listed, whether it is in
        stock. Nothing is generated, because a sentence about what an item is
        made of has to be true, and only the seller knows.
    """
    product_id = convo.context.get("product")
    product = db.get(Product, product_id) if isinstance(product_id, int) else None
    if product is None or product.seller_id != seller.id:
        return None

    parts = [f"*{product.title}*"]
    if product.price_display:
        parts.append(product.price_display)
    if product.description:
        parts.append(product.description)
    if product.sizes:
        parts.append("Sizes: " + ", ".join(product.sizes))
    parts.append("In stock." if product.stock > 0 else "_Sold out right now._")

    return [
        Reply(
            "\n".join(parts),
            buttons=[("add", "Add to basket"), ("ask", "Ask the shop"), ("menu", "Keep looking")],
        )
    ]


def _hand_off(
    db: Session, seller: Seller, phone: str, question: str, *, pending: str | None = None
) -> Outcome:
    """
    Give a question to the shop, and tell the buyer it is on its way.

    Args:
        db: Session. The caller commits.
        seller: The shop being asked.
        phone: Who is waiting.
        question: What they asked, verbatim.
        pending: What the buyer was in the middle of, re-offered underneath so
            asking a question does not cost them their place.

    Notes:
        THERE IS NO "I DON'T KNOW" HERE, and that is deliberate rather than
        aspirational. Delivery to a town, what it costs, when it arrives,
        whether something can be held back — none of it is in the system, and
        answering anyway would put a promise on the buyer's screen that the
        seller never made. So it goes to the person who can actually say.

        THE BUYER IS TOLD IT WAS ASKED, not that it was answered. "I'll bring
        their answer straight back" is a commitment the code keeps; anything
        warmer would be a guess about how fast the seller reads their phone.
    """
    row = ask(db, seller, buyer_phone=phone, question=question)

    told = f"Let me ask *{seller.display_name}* — I'll bring their answer straight back."
    if pending:
        told = f"{told}\n\n{pending}"

    alert = Reply(
        f"📩 A customer is asking:\n\n_{row.question}_\n\n"
        f"Reply and I'll pass it straight on to them.",
        buttons=[(f"answer:{row.id}", "Answer now"[:20])],
    )

    return Outcome([Reply(told)], notify=_tell_seller(seller, alert))


def _buyer_said_something(
    db: Session, seller: Seller, convo: WaConversation, said: str
) -> Outcome | None:
    """
    A buyer wrote a sentence. Work out what it was, or hand back None.

    Returns:
        What to do, or None when the model has nothing useful — in which case
        the caller runs the keyword search and the menu, exactly as before.

    Notes:
        IT RUNS AFTER THE FREE PATHS, NOT INSTEAD OF THEM. The literal search
        already answers "tote" and the budget parser already answers "under
        1000", both without spending anything. This is for the sentences those
        two miss — "do you have anything for a nine year old", "is there
        something cheaper", "asante" — which is where a shop either sounds like
        a person or does not.
    """
    reading = _understand(db, convo, said, owner=None, shopping_at=seller)
    if reading is None:
        return None

    if reading.intent is Intent.FIND_PRODUCT and not reading.query:
        return _ask_for(convo, "query")

    if reading.intent is Intent.BUDGET and not reading.max_price_kes:
        return _ask_for(convo, "budget")

    if reading.intent is Intent.FIND_PRODUCT and reading.query:
        found = get_public_products(db, seller, search=reading.query)[:PAGE_SIZE]
        if found:
            return Outcome(_found(db, seller, convo, found, heading=reading.query))
        return Outcome(
            [
                Reply(
                    f"I couldn't find anything matching *{reading.query}*.",
                    buttons=[("menu", "See what we have")],
                )
            ]
        )

    if reading.intent is Intent.BUDGET and reading.max_price_kes:
        found = get_public_products(db, seller, max_price_kes=reading.max_price_kes)[:PAGE_SIZE]
        if found:
            return Outcome(_found(db, seller, convo, found, ceiling=reading.max_price_kes))
        return Outcome(
            [
                Reply(
                    f"I don't have anything under *KES {reading.max_price_kes:,}* right now.",
                    buttons=[("menu", "See everything")],
                )
            ]
        )

    if reading.intent is Intent.ABOUT_THIS_ITEM:
        told = _about_this_item(convo, db, seller)
        if told is not None:
            return Outcome(told)
        # Nothing on screen to be asking about, so it is a question for the
        # shop like any other.
        return _hand_off(db, seller, convo.phone, reading.question or said)

    if reading.intent is Intent.FOR_THE_SELLER:
        return _hand_off(db, seller, convo.phone, reading.question or said)

    if reading.intent is Intent.VIEW_BASKET:
        return Outcome(_show_cart(db, seller, convo))

    if reading.intent is Intent.CHECKOUT:
        return Outcome(_start_checkout(db, seller, convo))

    if reading.intent is Intent.BROWSE:
        return Outcome(_menu(db, seller, convo))

    if reading.intent is Intent.GREET:
        return Outcome(_menu(db, seller, convo, greet=True))

    if reading.intent is Intent.ADD_TO_BASKET:
        product_id = convo.context.get("product")
        product = db.get(Product, product_id) if isinstance(product_id, int) else None
        if product is not None and product.seller_id == seller.id and product.stock > 0:
            if product.sizes:
                return Outcome(_ask_variant(convo, product))
            return Outcome(_add_to_basket(db, seller, convo, product))
        return Outcome(_menu(db, seller, convo))

    # Small talk, or a question with no action behind it. Answered in words,
    # with a way onward attached so it is never a dead end.
    if reading.may_speak and reading.reply:
        return Outcome(
            [
                Reply(
                    reading.reply,
                    buttons=[("menu", "See what we have"), ("cart", "My basket")],
                )
            ]
        )

    return None
