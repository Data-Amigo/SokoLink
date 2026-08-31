"""
The shop, as a conversation. No browser anywhere in it.

    inbound text ──▶ handle() ──▶ [Reply, ...] ──▶ TwiML in the webhook response

WHY THE CHAT IS THE SHOP. A web link is the one thing we cannot control: tapping
a URL hands an intent to the operating system, and some Android skins route it
to the default browser however the message was built. Tested on a real handset —
plain link and native CTA button both left WhatsApp. So the buyer surface stops
being a page we link to and becomes the thread itself, which no OS can redirect.

NATIVE COMPONENTS, WITH THE TEXT STILL UNDERNEATH. Replies carry buttons and
list rows, which Meta's Cloud API draws as real tap targets — free-form, inside
the 24-hour window a person opens by messaging first, with no template
approval. That approval requirement is why the first version of this file
shipped numbered text menus on Twilio.

The body ALWAYS names every option anyway. Twilio cannot render a component and
gets the text; so does any client that draws neither. A reply whose body reads
only "Choose:" is a dead end everywhere the buttons do not appear.

A TAP COMES BACK AS AN ID, a typed word as a word, and `handle` accepts both.
The ids are unambiguous in a way words are not — "cat:Shoes" cannot also be a
product name — but they are an addition, never a replacement.

EVERY REPLY ENDS BY SAYING WHAT TO SEND NEXT. There is no back button, no menu
bar and no visible affordance in a chat — the only interface is the last message
still on screen. A reply that does not name its options is a dead end.

WHAT THIS DOES NOT DECIDE. It never sets a price, never marks anything paid, and
never invents stock. It reads the catalogue and calls the same cart and order
services the web checkout calls, so the two surfaces cannot drift into
disagreeing about what an order is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Cart,
    ConversationState,
    Order,
    OrderStatus,
    PriceSource,
    Product,
    ProductStatus,
    Seller,
    WaConversation,
)
from app.services.accounts import SignupError, create_account_for_phone
from app.services.cart import CartError, add_item, clear, get_or_create_cart
from app.services.catalogue import PublishError, publish_product
from app.services.daraja import get_stk_engine
from app.services.intake import IntakeError, MediaFetch, ingest_forwarded_post
from app.services.media import absolute_url
from app.services.orders import (
    OrderError,
    claim_payment,
    confirm_payment,
    get_payment_method,
    place_order,
)
from app.services.payment_methods import PaymentSetupError, save_payment_method
from app.services.payments import MPESA_CALLBACK_PATH, PaymentError, start_stk_payment
from app.services.storefront import get_categories, get_public_products

#: How many sizes a picker may offer. Meta's list caps at ten rows, and a
#: catalogue with more sizes than that is a shop the chat cannot serve well
#: anyway — the storefront exists for those.
VARIANT_LIMIT = 10

#: How many numbered options one message may offer. Past this a buyer is
#: scrolling a wall of text on a phone and stops reading; categories exist
#: precisely so a catalogue never has to be shown flat.
PAGE_SIZE = 8


@dataclass(frozen=True)
class Reply:
    """
    One outbound WhatsApp message.

    Args:
        body: The text. ALWAYS meaningful on its own.
        media_url: An absolute image URL, or None. Relative paths cannot work —
            the provider fetches this from its own servers, not from a browser
            with our origin.
        buttons: ``(id, title)``, at most three, rendered as tap targets.
        rows: ``(id, title, description)`` for a list picker.
        list_label: The button that opens the list. Short; not a sentence.

    Notes:
        THE BODY IS NEVER JUST A HEADER FOR THE COMPONENT. Two providers render
        these differently — Meta draws real buttons, Twilio cannot and gets the
        options flattened into numbered text — and one of them may be a client
        that shows neither. A reply whose body is "Choose:" is a dead end
        wherever the component does not draw.

        WHICH IDS EXIST IS THE CONVERSATION'S BUSINESS, not the sender's. They
        are matched in `handle`, so a tap and a typed word take the same path.
    """

    body: str
    media_url: str | None = None
    buttons: list[tuple[str, str]] | None = None
    rows: list[tuple[str, str, str]] | None = None
    list_label: str = "Choose"

    #: An approved template to send instead of ``body``, as
    #: ``(name, body_params, button_param)``. Rarely needed — see ``link``.
    template: tuple[str, list[str], str] | None = None

    #: A URL to render as a BUTTON rather than as text, as ``(url, label)``.
    #:
    #: THE RIGHT WAY TO SEND A LINK, found after two wrong turns. Raw text goes
    #: to the device's default browser. A CTA button on an approved template
    #: opens in WhatsApp's own browser but needs Meta review and costs per send.
    #: An interactive cta_url does the same job with no template, no approval,
    #: and free inside the 24-hour window.
    #:
    #: ONE BUTTON PER MESSAGE — Meta's limit, not ours.
    link: tuple[str, str] | None = None


@dataclass
class Outcome:
    """What one inbound message produced."""

    replies: list[Reply] = field(default_factory=list)


def _digits(text: str) -> int | None:
    """The number a buyer typed, or None when they typed something else."""
    match = re.fullmatch(r"\s*(\d{1,3})\s*", text)
    return int(match.group(1)) if match else None


def _price(text: str) -> int | None:
    """
    A price a seller typed, or None.

    Separate from :func:`_digits`, which reads MENU choices and caps at three
    digits. A price is up to seven, and a seller writes it the way they say it —
    "1,800", "1800/=", "ksh 1800" all mean the same thing.

    Money here is integer KES, so a decimal point is refused rather than
    rounded: silently turning 1800.50 into 1800 loses fifty cents of somebody
    else's money without telling them.
    """
    cleaned = re.sub(r"(?i)^(ksh|kes|bei)\s*", "", text.strip())
    cleaned = cleaned.rstrip("/=").replace(",", "").strip()
    if not re.fullmatch(r"\d{1,7}", cleaned):
        return None
    value = int(cleaned)
    return value if value > 0 else None


def _money(amount: int) -> str:
    """Kenyan shillings, grouped, no decimals — money here is integer KES."""
    return f"KSh {amount:,}"


def get_conversation(db: Session, phone: str) -> WaConversation:
    """
    This buyer's conversation, created on first contact.

    Args:
        db: Session.
        phone: Bare digits with country code.

    Returns:
        The persisted conversation.
    """
    found = db.scalar(select(WaConversation).where(WaConversation.phone == phone))
    if found is None:
        found = WaConversation(phone=phone, state=ConversationState.NEW, context={})
        db.add(found)
        db.flush()
    return found


def _basket(db: Session, convo: WaConversation, seller: Seller) -> Cart:
    """
    This thread's basket, remembered across messages.

    Args:
        db: Session.
        convo: The conversation, which owns the token.
        seller: The shop.

    Returns:
        The persisted cart.

    Notes:
        ``get_or_create_cart`` MINTS A NEW TOKEN when it is not given one it
        recognises — the web flow relies on that and writes the token back to a
        cookie. A chat has no cookie, so the token is stored on the conversation
        instead. Passing the phone number in as a made-up token does not work:
        it is never found, so every message would silently get an empty basket.

        The token is also re-read after the call, because a shop switch mid-chat
        produces a different cart and the conversation must follow it.
    """
    cart = get_or_create_cart(db, convo.cart_token, seller)
    convo.cart_token = cart.token
    return cart


def _shop_card(seller: Seller) -> Reply:
    """
    The template message whose CTA button opens this shop INSIDE WhatsApp.

    Notes:
        A BUTTON, NOT A LINK IN THE TEXT. A raw URL in a message body is handed
        to the device's default browser every time. A cta_url is at least a
        button, and it is the only free way to send one.

        IT MAY STILL LEAVE WHATSAPP, and the docstring here used to claim
        otherwise. Meta's in-app browser is documented as applying to CTA
        buttons on approved MARKETING OR UTILITY TEMPLATES; Meta's own reference
        for interactive cta_url says the URL "will load in the device's default
        web browser". Support for interactive messages is announced as coming,
        not shipped. The template path (see whatsapp_shop_template) is the one
        with the documented guarantee, and it costs per send.

        WHICH IS WHY THIS IS NO LONGER THE THING A SELLER SHARES. The share link
        is a wa.me deep link — see _share_link — which cannot be routed to a
        browser at all, because it is not a page request. This card is for
        LOOKING at a shop from inside a conversation that already exists.
    """
    return Reply(
        f"*{seller.display_name}* — tap to browse everything they have.",
        link=(f"{settings.app_base_url}/shop/{seller.slug}", "Open shop"),
    )


def _greeting(seller: Seller) -> str:
    return f"*{seller.display_name}*\nKaribu! 👋"


def _menu(
    db: Session, seller: Seller, convo: WaConversation, *, arriving: bool = False
) -> list[Reply]:
    """
    The category menu, and the state that makes its numbers meaningful.

    Args:
        db: Session.
        seller: The shop.
        convo: The conversation, moved into BROWSING.
        arriving: True only on the buyer's FIRST message to this shop.

    Notes:
        THE GREETING IS AN ARRIVAL, NOT A HEADER. This used to open with the
        shop's name and "Karibu!" every single time — and because an
        unrecognised word falls back to here, a buyer mid-purchase who typed
        something we could not read was greeted again, from the top, as though
        they had just walked in. Nothing else in the thread said "robot" louder.

        A shop introduces itself once. After that it is simply who you are
        talking to, and repeating the name on every message is what a machine
        does to compensate for not being a person.

        A shop with no categories skips straight to the product list rather than
        showing an empty menu — a seller who never typed a category should not
        cost their buyer a dead screen.
    """
    categories = get_categories(db, seller)[:PAGE_SIZE]
    if not categories:
        return _greet_then(db, seller, convo, arriving=arriving)

    convo.state = ConversationState.BROWSING
    convo.context = {"options": categories}

    # The body still lists every option. Meta draws the rows as a picker, but a
    # buyer on a cheap handset replying "2" is a real pattern here, and the
    # numbers are what make that work.
    lines = [f"{i}. {name}" for i, name in enumerate(categories, start=1)]
    question = "What are you looking for?"
    opening = f"{_greeting(seller)}\n\n{question}" if arriving else question
    return [
        Reply(
            f"{opening}\n\n" + "\n".join(lines),
            rows=[(f"cat:{name}", name, "") for name in categories],
            list_label="Browse",
        )
    ]


def _greet_then(
    db: Session, seller: Seller, convo: WaConversation, *, arriving: bool = False
) -> list[Reply]:
    """Everything in the shop — for a shop whose seller never typed a category."""
    replies = _list_products(db, seller, convo, category=None)
    if not arriving:
        return replies

    first = replies[0]
    return [
        Reply(
            f"{_greeting(seller)}\n\n{first.body}",
            media_url=first.media_url,
            buttons=first.buttons,
            rows=first.rows,
            list_label=first.list_label,
        ),
        *replies[1:],
    ]


def _list_products(
    db: Session, seller: Seller, convo: WaConversation, category: str | None
) -> list[Reply]:
    """A numbered page of products, and the state that decodes the reply."""
    products = get_public_products(db, seller, category=category)[:PAGE_SIZE]

    if not products:
        convo.state = ConversationState.BROWSING
        convo.context = {}
        return [
            Reply(
                "Nothing in there just yet.",
                buttons=[("menu", "See everything else")],
            )
        ]

    convo.state = ConversationState.LISTING
    convo.context = {"products": [p.id for p in products], "category": category}

    lines = []
    rows = []
    for i, product in enumerate(products, start=1):
        price = product.price_display or "Ask for price"
        sold_out = " — *sold out*" if product.stock <= 0 else ""
        lines.append(f"{i}. {product.title} — {price}{sold_out}")
        # The price goes in the row DESCRIPTION, not the title: a title has 24
        # characters and the item's name needs all of them.
        rows.append(
            (
                f"prod:{product.id}",
                product.title,
                f"{price} — sold out" if product.stock <= 0 else price,
            )
        )

    heading = category or "Everything"
    body = f"*{heading}*\n\n" + "\n".join(lines)
    return [Reply(body, rows=rows, list_label="See items")]


def _show_product(convo: WaConversation, product: Product) -> list[Reply]:
    """
    One product, with its photo, and the things a buyer can do next.

    Notes:
        THE BUTTONS ARE THE INSTRUCTIONS. This used to end with "Send *add* to
        put this in your basket, or *menu* to keep looking" — printed directly
        above buttons that already said Add to basket and Keep looking. The
        narration is what a system writes when it cannot draw a control; we can
        draw the control, and saying it twice is how a shop sounds like a form.
    """
    convo.state = ConversationState.PRODUCT
    convo.context = {"product": product.id}

    parts = [f"*{product.title}*"]
    if product.price_display:
        parts.append(product.price_display)
    if product.description:
        parts.append(product.description)

    if product.stock <= 0:
        parts.append("\n_Sold out right now._")
        buttons = [("menu", "See what's in stock")]
    else:
        if product.sizes:
            parts.append("Sizes: " + ", ".join(product.sizes))
        buttons = [("add", "Add to basket"), ("menu", "Keep looking"), ("cart", "My basket")]

    return [
        Reply(
            "\n".join(parts),
            media_url=absolute_url(product.cover_url),
            buttons=buttons,
        )
    ]


def _ask_variant(convo: WaConversation, product: Product) -> list[Reply]:
    """
    Which size? Asked BEFORE the basket, because afterwards is too late.

    Notes:
        THE HOLE THIS CLOSES. The card printed the sizes and then added the item
        without asking which — so a seller opened an order for sandals with no
        size on it and had to go back to the buyer to find out. The column has
        been on the cart line and the order line the whole time; only the
        question was missing.

        ROWS RATHER THAN BUTTONS. Three buttons is Meta's limit and shoes come
        in more sizes than that. A list picker holds ten, which covers every
        real case, and the body lists them too so a typed "40" also works.
    """
    convo.state = ConversationState.VARIANT
    convo.context = {"product": product.id, "sizes": list(product.sizes)}

    return [
        Reply(
            f"*{product.title}*\n\nWhich size do you need?\n\n"
            + "  ".join(product.sizes[:VARIANT_LIMIT]),
            rows=[
                (f"size:{size}", f"Size {size}"[:24], "") for size in product.sizes[:VARIANT_LIMIT]
            ],
            list_label="Pick a size",
        )
    ]


def _add_to_basket(
    db: Session, seller: Seller, convo: WaConversation, product: Product, variant: str = ""
) -> list[Reply]:
    """
    Put one item in the basket and show what is now in it.

    Notes:
        ONE MESSAGE, NOT TWO. The old version sent "Added ✅" and then appended
        ``_show_cart(...)[1:]`` — a slice written when the basket was two
        messages, which by then returned exactly one. So the slice was always
        empty: the buyer got a bare sentence with NO buttons, and had to type
        their way to checkout. That is the bug in the screenshot.

        Acknowledgement and next step belong together anyway. A shopkeeper says
        "got it — that's two things, four thousand, shall we ring it up?", not
        "got it" followed by silence.
    """
    try:
        cart = _basket(db, convo, seller)
        add_item(db, cart, product.id, quantity=1, selected_variant=variant)
    except CartError:
        # Sold out, or gone from the catalogue since we showed it.
        return [
            Reply(
                "That's just gone, sorry — someone got there first.",
                buttons=[("menu", "See what's left")],
            )
        ]

    named = f"{product.title} ({variant})" if variant else product.title
    replies = _show_cart(db, seller, convo)
    replies[0] = Reply(
        f"Added *{named}*. ✅\n\n{replies[0].body}",
        buttons=replies[0].buttons,
        rows=replies[0].rows,
        list_label=replies[0].list_label,
    )
    return replies


def _show_cart(db: Session, seller: Seller, convo: WaConversation) -> list[Reply]:
    """The basket, and the decision it exists to prompt."""
    cart = _basket(db, convo, seller)

    if not cart.items:
        convo.state = ConversationState.BROWSING
        convo.context = {}
        return [
            Reply(
                "Your basket is empty.",
                buttons=[("menu", "Start shopping")],
            )
        ]

    convo.state = ConversationState.CART
    convo.context = {}

    lines = []
    for item in cart.items:
        # The cart reads through to the live product on purpose. Only an ORDER
        # line copies the name and price, so a rename mid-basket is fine but a
        # rename after checkout can never rewrite what somebody paid.
        variant = f" ({item.selected_variant})" if item.selected_variant else ""
        lines.append(
            f"• {item.product.title}{variant} × {item.quantity} — {_money(item.line_total_kes)}"
        )

    return [
        Reply(
            "*Your basket*\n\n" + "\n".join(lines) + f"\n\nTotal: *{_money(cart.subtotal_kes)}*",
            buttons=[
                ("checkout", "Checkout"),
                ("menu", "Keep shopping"),
                ("clear", "Empty basket"),
            ],
        )
    ]


def _start_checkout(db: Session, seller: Seller, convo: WaConversation) -> list[Reply]:
    """
    Begin checkout by asking the first of three short questions.

    Notes:
        ONE QUESTION PER MESSAGE. This used to be a single message asking for
        "your name and where to deliver, separated by a comma" — which is a form
        pasted into a chat, and it fails in the ordinary case of somebody whose
        estate has a comma in its name. Three small questions cost three round
        trips and no thinking; one compound question costs one round trip and
        an error message.

        NOTHING IS ASKED IF THE MONEY HAS NOWHERE TO GO. A shop with no payment
        method cannot take an order, and finding that out after typing a name
        and an address is the worst possible moment to be told.
    """
    cart = _basket(db, convo, seller)
    if not cart.items:
        return _show_cart(db, seller, convo)

    if get_payment_method(db, seller) is None:
        return [
            Reply(
                f"{seller.display_name} hasn't set up M-Pesa yet, so I can't take "
                "the order just yet.",
                buttons=[("menu", "Keep looking")],
            )
        ]

    convo.state = ConversationState.CHECKOUT_NAME
    convo.context = {}
    return [Reply("Lovely. Who is this order for?\n\n_Just a name is fine._")]


def _ask_delivery(convo: WaConversation, name: str) -> list[Reply]:
    """
    Delivered, or collected? The question that was never asked at all.

    Notes:
        IT WAS ASSUMED. Checkout demanded an address from everybody, including
        buyers who meant to collect — and the seller then had a delivery
        address for an order nobody was delivering.

        NO FEE IS ADDED HERE. The order carries delivery_fee_kes and it stays
        zero: what delivery costs in Nairobi depends on where, and inventing a
        number would put a price on the buyer's screen that the seller never
        agreed to. The two of them settle it in the thread, which is what
        already happens today.
    """
    convo.state = ConversationState.CHECKOUT_DELIVERY
    convo.context = {"buyer_name": name}
    return [
        Reply(
            f"Thanks, {name}. Would you like it delivered, or will you collect it?",
            buttons=[("deliver", "Deliver to me"), ("collect", "I'll collect")],
        )
    ]


def _ask_address(convo: WaConversation) -> list[Reply]:
    """Where to. Asked only of buyers who said they wanted it delivered."""
    convo.state = ConversationState.ADDRESS
    return [
        Reply(
            "Where should it go?\n\n_An estate or landmark is enough — "
            "like Kasarani, near Hunters._"
        )
    ]


def _place(
    db: Session,
    seller: Seller,
    convo: WaConversation,
    phone: str,
    *,
    name: str,
    address: str | None,
) -> list[Reply]:
    """Turn the basket into an order, and tell them exactly how to pay."""
    cart = _basket(db, convo, seller)
    if not cart.items:
        return _show_cart(db, seller, convo)

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
        return [Reply(str(exc), buttons=[("menu", "Keep shopping")])]

    db.flush()

    convo.state = ConversationState.PAYING
    convo.order_reference = order.reference
    convo.context = {}

    return _ask_for_payment(db, seller, order)


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


def _claim(db: Session, convo: WaConversation, text: str) -> list[Reply]:
    """
    Record the code the buyer says they paid with.

    A CODE IS A CLAIM, NOT A PAYMENT. Nothing here marks the order paid — only
    the seller does that, from the workspace, after checking their own M-Pesa
    messages. Saying "payment received" at this point would be a lie the buyer
    would believe.
    """
    code = text.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,15}", code):
        return [
            Reply(
                "That doesn't look like an M-Pesa code. It's the reference in "
                "the M-Pesa message, like _SLK7XA2B9C_.\n\n"
                "Send it here once you've paid."
            )
        ]

    order = db.scalar(select(Order).where(Order.reference == convo.order_reference))
    if order is None:
        convo.state = ConversationState.BROWSING
        convo.order_reference = None
        return [Reply("I've lost track of that order. Send *menu* to start again.")]

    claim_payment(db, order, code=code)
    return [
        Reply(
            f"Got it — *{code}* recorded against order *{order.reference}*.\n\n"
            "The seller will confirm once the money shows in their M-Pesa. "
            "You'll get a message here when they do."
        )
    ]


# ══════════════════════════════════════════════════════════════════════════
# THE SELLER SIDE
# ══════════════════════════════════════════════════════════════════════════
#
# A message from a number that owns a shop is a SELLER, not a buyer. This is the
# product's central promise: they forward a catalogue post from the WhatsApp
# group they already sell in, and it becomes a product.
#
# Sellers are recognised by their own WhatsApp number, which is the identity
# they sign in with. Nobody types a command to "switch mode" — a seller
# forwarding a photo means one thing, and asking them to announce it first would
# be friction we invented for our own convenience.


def find_seller_by_phone(db: Session, phone: str) -> Seller | None:
    """
    The shop this number owns, if any.

    Args:
        db: Session.
        phone: Bare digits with country code.

    Returns:
        The seller, or None for an ordinary buyer.

    Notes:
        Matched on the number as stored AND without a country code, because a
        seller may have typed theirs either way in the workspace and neither is
        wrong to them.
    """
    local = "0" + phone[3:] if phone.startswith("254") and len(phone) == 12 else phone
    return db.scalar(select(Seller).where(Seller.whatsapp_number.in_({phone, local, f"+{phone}"})))


def _handle_forward(
    db: Session, seller: Seller, media_id: str, fetch: MediaFetch, caption: str
) -> tuple[list[Reply], int | None]:
    """
    Turn one forwarded catalogue post into a draft product, and say what happened.

    THE REPLY NAMES THE ITEM AND ASKS FOR WHAT IS MISSING. "Product created" is
    useless: a seller forwarding twenty posts needs to know which one this was
    and whether it still needs them. Prices are usually absent — verified
    against 24 real captions, zero mention KSh — so "needs a price" is the
    common case, not an error.
    """
    try:
        result = ingest_forwarded_post(db, seller, media_id=media_id, fetch=fetch, caption=caption)
    except IntakeError as exc:
        # The message is written to be shown to a seller verbatim.
        return [Reply(str(exc))], None

    product = result.product
    if result.needs_price:
        # The CALLER queues it and asks. Asking "reply with the price" here,
        # once per photo, would put eight unanswerable questions in a row on a
        # seller's screen — there would be no way to tell which one a number
        # was answering.
        return [Reply(f"Added *{product.title}* to your drafts.")], product.id

    return [Reply(f"Added *{product.title}* — {product.price_display}.")], None


def _pricing_prompt(db: Session, seller: Seller, convo: WaConversation) -> list[Reply]:
    """
    Ask for the price of the next draft in the queue, or wrap up.

    Args:
        db: Session.
        convo: The seller's conversation, carrying the queue in context.

    Returns:
        The next question, or the summary once nothing is left to price.

    Notes:
        ONE ITEM AT A TIME, BY NAME. A seller who forwards eight posts and is
        then asked for "the prices" has to remember an order we never showed
        them. Naming the item makes a bare number unambiguous, which is the
        only way a one-word reply can be safe to act on.

        The queue is re-checked against the database rather than trusted: a
        seller may have priced something in the workspace between messages, and
        asking again for a price they already set reads as the bot not paying
        attention.
    """
    queue: list[int] = [int(pid) for pid in convo.context.get("pricing_queue", [])]

    while queue:
        product = db.get(Product, queue[0])
        if product is None or product.price_kes is not None:
            queue.pop(0)
            continue
        convo.context = {**convo.context, "pricing_queue": queue}
        remaining = f" ({len(queue)} left)" if len(queue) > 1 else ""
        return [
            Reply(
                f"What's the price for *{product.title}*?{remaining}\n\n"
                f"Just the number in shillings — or send *skip* to leave it for later."
            )
        ]

    # Nothing left needing a price.
    convo.state = ConversationState.NEW
    convo.context = {}
    return _priced_summary(db, seller)


def _priced_summary(db: Session, seller: Seller) -> list[Reply]:
    """
    Tell the seller what is now ready, and offer the one action that follows.

    A DRAFT WITH A PRICE IS STILL NOT FOR SALE. Publishing stays a deliberate
    act — this offers it in one word rather than sending them to a browser,
    which is the whole point of the WhatsApp surface.
    """
    ready = db.scalars(
        select(Product).where(
            Product.seller_id == seller.id,
            Product.status == ProductStatus.DRAFT.value,
            Product.price_kes.is_not(None),
        )
    ).all()

    if not ready:
        return [Reply("All done. ✅ Send another photo whenever you have new stock.")]

    lines = "\n".join(f"• {p.title} — {p.price_display}" for p in ready[:PAGE_SIZE])
    it = "it" if len(ready) == 1 else "them"
    return [
        Reply(
            f"Ready to go live:\n\n{lines}\n\n"
            f"Send *publish* to put {it} in your shop, or leave {it} as a draft."
        )
    ]


def _set_price(db: Session, seller: Seller, convo: WaConversation, amount: int) -> list[Reply]:
    """
    Apply a price the seller typed to the draft we asked about.

    Args:
        db: Session.
        convo: Carries which product we asked about.
        amount: Whole shillings.

    Returns:
        Confirmation, then the next question.

    Notes:
        THE PRICE IS THE SELLER'S, NEVER THE MODEL'S GUESS. This is the other
        half of the rule that keeps a wrong price off a buyer's screen: the
        agent reports only what it could see, and the human supplies the rest.
    """
    queue: list[int] = [int(pid) for pid in convo.context.get("pricing_queue", [])]
    if not queue:
        return _pricing_prompt(db, seller, convo)

    product = db.get(Product, queue[0])
    if product is None:
        convo.context = {**convo.context, "pricing_queue": queue[1:]}
        return _pricing_prompt(db, seller, convo)

    product.price_kes = amount
    # The seller typed it, so the source is neither the caption nor the image.
    product.price_source = PriceSource.SELLER.value
    product.price_evidence = None
    db.flush()

    convo.context = {**convo.context, "pricing_queue": queue[1:]}
    return [Reply(f"*{product.title}* — {product.price_display}. ✅")] + _pricing_prompt(
        db, seller, convo
    )


#: What a seller might type or tap when asked how they take money. The button
#: ids are the canonical form; the words are what somebody types instead.
_PAY_ALIASES: dict[str, str] = {
    "pochi": "pochi",
    "pochi la biashara": "pochi",
    "till": "till",
    "till number": "till",
    "buy goods": "till",
    "paybill": "paybill",
    "pay bill": "paybill",
}


def _find_shop(db: Session, wanted: str) -> Seller | None:
    """
    The shop a buyer named, by slug or by the name on the sign.

    Notes:
        BOTH, BECAUSE THE LINK CARRIES THE NAME. The share link prefills "Shop
        Book Lounge" rather than "shop book-lounge" — the buyer's own first
        message appears in their chat, and it should read like something a
        person wrote, not like a command they mistyped. Matching the slug too
        keeps every link already in circulation working.

        CLOSED SHOPS ARE NOT FOUND. A buyer must never reach a catalogue whose
        owner has not opened it; that is the publish gate, and a chat that
        ignored it would be a way around it.
    """
    cleaned = wanted.strip()
    if not cleaned:
        return None
    return db.scalar(
        select(Seller).where(
            Seller.is_published.is_(True),
            or_(
                Seller.slug == cleaned.lower(),
                func.lower(Seller.display_name) == cleaned.lower(),
            ),
        )
    )


def _resume_pricing(db: Session, seller: Seller, convo: WaConversation) -> list[Reply]:
    """
    Pick the pricing queue back up, for a seller who left it half-done.

    Notes:
        A SELLER WHO WALKS AWAY MID-QUEUE MUST BE ABLE TO COME BACK. Prices are
        asked for one at a time immediately after a forward, and a seller
        interrupted by a customer loses the thread entirely — the drafts sit
        there, invisible, and the shop looks broken for a reason nothing
        explains. This rebuilds the queue from what is actually unpriced rather
        than from whatever the conversation last remembered.
    """
    unpriced = [
        int(pid)
        for pid in db.scalars(
            select(Product.id)
            .where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.DRAFT.value,
                Product.price_kes.is_(None),
            )
            .order_by(Product.id)
        ).all()
    ]

    if not unpriced:
        return [
            Reply(
                "Everything you've sent me has a price. 👍",
                buttons=[("publish", "Add to my shop")],
            )
        ]

    convo.state = ConversationState.PRICING
    convo.context = {"pricing_queue": unpriced}
    return _pricing_prompt(db, seller, convo)


def _plural(count: int, word: str) -> str:
    """``1 item`` / ``3 items``. Copy that says "1 items" reads as a bug."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _share_link(seller: Seller) -> str | None:
    """
    The one link a seller shares. A wa.me deep link, never a web URL.

    Returns:
        The link, or None when the bot's own number is not configured.

    Notes:
        WHY A DEEP LINK AND NOT THE STOREFRONT URL. Tapping an http(s) URL hands
        an intent to the operating system, which is free to open it in Chrome —
        and does. A wa.me link is not a page request, it opens WhatsApp itself,
        so nothing can route it away from the app the whole product lives in.

        It also lands the buyer in a CONVERSATION, which opens the 24-hour
        window. Inside that window we may send interactive messages and buttons
        for free — including the one offering the full storefront. Arriving by
        web URL gives us a page view and no way to speak to them.

        NONE RATHER THAN A GUESS. A share link built on a missing number is a
        seller posting a dead link to their status, which is worse than being
        told we cannot build one yet.
    """
    number = (settings.whatsapp_display_number or "").strip().lstrip("+")
    if not number:
        return None
    return f"https://wa.me/{number}?text={quote(f'Shop {seller.display_name}')}"


def _share_card(seller: Seller) -> Reply:
    """
    The seller's link, as text they can copy.

    Notes:
        TEXT, NOT A BUTTON, and that is the whole point. A cta_url button can be
        tapped but not copied, and this link's entire job is to be pasted into a
        status, a bio and half a dozen groups. A button here would be a control
        that looks right and cannot do the one thing it exists for.
    """
    link = _share_link(seller)
    if link is None:
        return Reply(
            "I can't build your link yet — our WhatsApp number isn't set up on "
            "our side. That one's ours to fix, not yours. Try again shortly."
        )
    return Reply(
        "Here's your shop link. Put it in your WhatsApp status, your bio, and "
        f"the groups you already sell in:\n\n{link}\n\n"
        "Anyone who taps it opens a chat with me, and I show them your shop "
        "straight away — they never leave WhatsApp."
    )


def _publish_ready(db: Session, seller: Seller) -> list[Reply]:
    """
    Put every priced item into the shop, and say what a buyer can now reach.

    Notes:
        IT GOES THROUGH THE SAME GATE the workspace uses. publish_product
        refuses an unpriced item and names it, so the chat cannot become a
        second way to go live that skips the rule.

        THE SHOP ITSELF IS NOT OPENED HERE. Putting an item in the shop and
        opening the shop are different decisions, and a seller who has not seen
        their shop should not discover it is live because they priced a shirt.
    """
    drafts = db.scalars(
        select(Product).where(
            Product.seller_id == seller.id,
            Product.status == ProductStatus.DRAFT.value,
            Product.price_kes.is_not(None),
        )
    ).all()

    if not drafts:
        return [
            Reply(
                "Nothing has a price yet, so there's nothing I can put in your "
                "shop. Forward me a photo of something you're selling and I'll "
                "set it up."
            )
        ]

    published = []
    for product in drafts:
        try:
            publish_product(db, seller, product.id)
            published.append(product.title)
        except PublishError as exc:
            return [Reply(str(exc))]

    names = "\n".join(f"• {title}" for title in published[:PAGE_SIZE])

    if seller.is_published:
        return [Reply(f"In your shop now:\n\n{names}"), _shop_card(seller)]

    return [
        Reply(
            f"Added to your shop:\n\n{names}\n\n"
            "Your shop is still closed, so nobody can see them yet.",
            buttons=[("open", "Open for business")],
        )
    ]


def _open_shop(db: Session, seller: Seller) -> list[Reply]:
    """
    Open the shop for business, if it is ready to be open.

    Notes:
        TWO GUARDS, AND BOTH ARE ABOUT NOT BREAKING A PROMISE TO A BUYER. An
        empty shop wastes the tap. A shop with no payment method is worse: the
        buyer chooses, commits, reaches checkout and finds no way to send money
        — which costs the seller a customer they had already won.

        EACH REFUSAL CARRIES THE FIX AS A BUTTON. Telling somebody they are
        blocked without handing them the next step is how a seller decides the
        thing does not work.

        THE DATABASE ALSO REFUSES a published shop with no WhatsApp number. That
        rail cannot fire here — a seller is RECOGNISED by their number, so one
        without a number never reaches this function as a seller at all. It
        guards the workspace, where a shop can be opened from an email login.
    """
    live = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.PUBLISHED.value,
            )
        )
        or 0
    )
    if not live:
        return [
            Reply(
                "There's nothing in your shop yet, so opening it would show "
                "buyers an empty room. Forward me a photo of something you're "
                "selling and I'll set it up."
            )
        ]

    if get_payment_method(db, seller) is None:
        return [
            Reply(
                "Before you open — how do buyers pay you?\n\n"
                "Without this they'd reach your shop, choose what they want, "
                "and find no way to send you money.",
                buttons=[("payments", "Set that up")],
            )
        ]

    seller.is_published = True
    db.flush()
    return [Reply(f"*{seller.display_name}* is open for business. 🎉"), _share_card(seller)]


def _ask_payment_kind(convo: WaConversation) -> list[Reply]:
    """
    How does this seller take M-Pesa?

    Notes:
        THREE BUTTONS BECAUSE THERE ARE THREE ANSWERS, and the difference is not
        cosmetic: Daraja's STK and C2B APIs work with paybill and buy-goods
        shortcodes only, so a Pochi seller can never have automatic
        confirmation. What is chosen here decides the whole checkout path.

        POCHI IS LISTED FIRST, deliberately. It needs no business registration,
        which is exactly why most Kenyan micro-sellers use it, and a list that
        buries it reads as though the common case were the exception.
    """
    convo.state = ConversationState.PAY_KIND
    convo.context = {}
    return [
        Reply(
            "How do your buyers pay you?\n\n"
            "*Pochi la Biashara* — the number on your phone\n"
            "*Till* — Buy Goods, usually 6 digits\n"
            "*Paybill* — a business number plus an account\n\n"
            "Tap one, or just type the word.",
            buttons=[
                ("pay:pochi", "Pochi la Biashara"),
                ("pay:till", "Till number"),
                ("pay:paybill", "Paybill"),
            ],
        )
    ]


#: What to ask for once the kind is known, and the example that makes the
#: question answerable. A prompt with no sample format is a guessing game.
_PAY_PROMPTS: dict[str, str] = {
    "pochi": (
        "What number is your Pochi la Biashara on?\n\n"
        "_Like 0712345678 — the number that receives the money._"
    ),
    "till": ("What's your till number?\n\n_The Buy Goods number, usually 6 digits — like 123456._"),
    "paybill": "What's your paybill number?\n\n_The business short code — like 400200._",
}


def _ask_payment_number(convo: WaConversation, kind: str) -> list[Reply]:
    """Record which kind was chosen, then ask for the number it needs."""
    convo.state = ConversationState.PAY_NUMBER
    convo.context = {"pay_kind": kind}
    return [Reply(_PAY_PROMPTS[kind])]


def _save_payment(db: Session, seller: Seller, convo: WaConversation, said: str) -> list[Reply]:
    """
    Take the number, validate it, and save how this seller gets paid.

    Notes:
        A PAYBILL NEEDS TWO ANSWERS, so this function is asked twice for one.
        The first reply is the short code, the second the account reference —
        held in the conversation context between them rather than in a third
        state, because they are two halves of one question.

        NOTHING IS SAVED UNTIL IT VALIDATES. save_payment_method owns the format
        rules, and its error text is already written for a seller, so it is
        shown verbatim rather than replaced with something vaguer.

        CREDENTIALS ARE NOT ASKED FOR HERE. A Daraja consumer secret is a long
        opaque string nobody should paste into a chat thread, and typing one on
        a phone is a transcription error waiting to happen. Automatic
        confirmation stays in the workspace; this path gets the seller PAID,
        which is the part that cannot wait.
    """
    kind = str(convo.context.get("pay_kind", ""))
    if kind not in _PAY_PROMPTS:
        # Context lost — a restart mid-answer, or a stale conversation row.
        # Asking again beats saving a number against a kind we are guessing.
        return _ask_payment_kind(convo)

    answer = said.strip()

    if kind == "paybill" and "pay_number" not in convo.context:
        convo.context = {**convo.context, "pay_number": answer}
        return [
            Reply(
                "And what account number should buyers use?\n\n"
                "_If your paybill doesn't need one, send *none*._"
            )
        ]

    number = str(convo.context.get("pay_number", answer))
    reference: str | None = None
    if kind == "paybill":
        reference = None if answer.lower() in {"none", "n/a", "-"} else answer

    try:
        method = save_payment_method(
            db,
            seller,
            kind=kind,
            number=number,
            account_name=seller.display_name,
            account_reference=reference,
        )
    except PaymentSetupError as exc:
        # They stay in this state: the format was wrong, and the fix is another
        # attempt, not being thrown back to the start of setup.
        return [Reply(f"{exc}\n\nTry again.")]

    convo.state = ConversationState.NEW
    convo.context = {}

    shown = f"{method.kind.title()} · {method.number}"
    if method.account_reference:
        shown += f" · account {method.account_reference}"

    replies = [Reply(f"Saved. Buyers will pay you on:\n\n*{shown}*")]

    if not seller.is_published:
        replies.append(
            Reply(
                "That's the last thing you needed. Ready to open?",
                buttons=[("open", "Open for business")],
            )
        )
    return replies


def _seller_orders(db: Session, seller: Seller) -> list[Reply]:
    """
    Every order waiting on this seller to say the money arrived.

    Notes:
        THIS IS THE SCREEN THAT MATTERS DAILY. Adding stock is a weekly job;
        being paid is the reason anybody uses this. It was missing entirely — a
        seller could be told an order existed and then had no way to look at it
        again.

        ONLY THE SELLER MOVES AN ORDER TO PAID, which is why this is a list of
        things to confirm rather than a receipt. A buyer typing an M-Pesa code
        is making a CLAIM; the money landing on the seller's phone is the fact.

        A ROW PER ORDER, capped at what a picker can hold. Past ten there is a
        backlog the chat cannot solve, and the newest are the ones whose buyer
        is still waiting with their phone in their hand.
    """
    orders = list(
        db.scalars(
            select(Order)
            .where(
                Order.seller_id == seller.id,
                Order.status == OrderStatus.AWAITING_CONFIRMATION.value,
            )
            .order_by(Order.created_at.desc())
            .limit(10)
        ).all()
    )

    if not orders:
        return [
            Reply(
                "Nothing waiting on you. 👍\n\n"
                "When a buyer says they've paid, the order lands here and you "
                "confirm it once the money shows up on your phone."
            )
        ]

    lines = [f"*{_plural(len(orders), 'order')} waiting on you*", ""]
    rows: list[tuple[str, str, str]] = []

    for order in orders:
        claimed = order.payments[-1].claimed_code if order.payments else None
        lines.append(f"*{order.reference}* · KES {order.total_kes:,} · {order.buyer_name}")
        lines.append(f"They sent code {claimed}" if claimed else "No code sent yet")
        lines.append("")
        rows.append(
            (
                f"confirm:{order.reference}",
                f"Paid · {order.reference}"[:24],
                f"KES {order.total_kes:,} · {order.buyer_name}"[:72],
            )
        )

    lines.append("Check your M-Pesa messages, then confirm the ones that arrived.")

    return [Reply("\n".join(lines), rows=rows, list_label="Confirm paid")]


def _confirm_order(db: Session, seller: Seller, reference: str) -> list[Reply]:
    """
    The seller vouches that one order's money arrived.

    Args:
        db: Session. The caller commits.
        seller: Who is confirming — checked against the order's owner.
        reference: The order reference from the row they tapped.

    Notes:
        SCOPED TO THIS SELLER, and that check is the point rather than a
        formality. An order reference travels: it is in the buyer's receipt and
        in this thread. Without the ownership test, anybody holding one could
        mark somebody else's order paid — the most damaging write in the system,
        because it closes an order and tells a buyer they are square.

        THE SERVICE OWNS THE RULES. confirm_payment refuses an order that is
        already closed and refuses one with no evidence behind it, so the chat
        cannot become a second, laxer way to settle money.
    """
    order = db.scalar(
        select(Order).where(Order.reference == reference, Order.seller_id == seller.id)
    )
    if order is None:
        return [Reply("I can't find that order. Send *orders* to see what's waiting.")]

    try:
        confirm_payment(db, order)
    except OrderError as exc:
        return [Reply(str(exc))]

    return [
        Reply(
            f"*{order.reference}* is paid. ✅\n\nKES {order.total_kes:,} from {order.buyer_name}.",
            buttons=[("orders", "What else is waiting")],
        )
    ]


def _welcome(convo: WaConversation) -> list[Reply]:
    """
    What a number we have never seen before is asked.

    Notes:
        IT ASKS WHICH THEY ARE, because the bot number serves both sides and a
        stranger's first message cannot tell us. Guessing wrong is expensive in
        both directions: a buyer walked through shop setup abandons, and a
        seller told to "open a shop's link" has been handed a riddle.

        NOT "SEND START". That was a placeholder in an early sketch and it read
        like a vending machine. People arriving here are already mid-conversation
        in their own heads.
    """
    convo.state = ConversationState.NEW
    convo.context = {}
    return [
        Reply(
            "Karibu! 👋 I'm Biashara Mall.\n\n"
            "I turn your WhatsApp catalogue into a shop people can buy from — "
            "you forward the posts, I do the rest. Buyers pay you on M-Pesa, "
            "directly.\n\n"
            "Are you here to sell, or to shop?",
            buttons=[("sell", "I want to sell"), ("buy", "I'm shopping")],
        )
    ]


def _ask_shop_name(convo: WaConversation) -> list[Reply]:
    """
    The one question onboarding actually needs answered.

    ONE FIELD, AND IT IS THE ONE THEY ALREADY KNOW. No email, no password, no
    category, no address. The number is already proven — the message arrived
    from it through a signed webhook — so a name is the entire remaining gap
    between a stranger and a shop.
    """
    convo.state = ConversationState.NAMING
    convo.context = {}
    return [
        Reply(
            "Nzuri! What's your shop called?\n\n"
            "_Whatever your customers already call you — like Zuma Fashion or "
            "Vitabu Bora._"
        )
    ]


def _create_shop(db: Session, convo: WaConversation, phone: str, name: str) -> list[Reply]:
    """
    Turn the answer into a real shop.

    Args:
        db: Session. The caller commits.
        convo: The conversation, moved out of NAMING on success.
        phone: The seller's number, proven by the signed webhook that carried
            this message.
        name: What they typed.

    Returns:
        A confirmation naming the shop and the one thing to do next, or the
        question again when the name cannot be used.

    Notes:
        THE NUMBER IS ALREADY PROVEN, which is why this can skip the OTP the web
        signup uses. Meta runs the network and told us who sent the message, and
        the payload's signature proves the message came from Meta. That is at
        least as strong as an SMS code we sent to a number somebody typed.

        THE SHOP IS CREATED CLOSED, with nothing in it. Opening is a deliberate
        act and stays one; a shop that opened itself at signup would be live and
        empty, which is worse than not existing.

        ONE INSTRUCTION, NOT THREE. They have just typed a name and are waiting
        to find out whether this works. Listing pricing, payments and opening
        here would all be true, and would also be the moment they stop reading.
    """
    clean = name.strip()
    if len(clean) < 2 or len(clean) > 60:
        return [
            Reply(
                "That name won't work — it needs to be between 2 and 60 "
                "characters. What should I call your shop?"
            )
        ]

    try:
        account = create_account_for_phone(db, phone=phone, shop_name=clean)
    except SignupError as exc:
        # Names the actual problem: taken, or unusable as a web address.
        return [Reply(f"{exc}\n\nTry another name.")]

    db.flush()
    seller = account.seller
    assert seller is not None

    convo.state = ConversationState.NEW
    convo.context = {}

    return [
        Reply(
            f"*{seller.display_name}* is yours. 🎉\n\n"
            "It's closed for now — nobody sees it until you say so.\n\n"
            "Now forward me a post from your catalogue. Photo and caption, "
            "exactly as you'd send a customer. I'll read it and set the item "
            "up, and if I can't see a price I'll ask you for one."
        )
    ]


def _seller_home(db: Session, seller: Seller) -> list[Reply]:
    """
    The seller's business, in one message.

    Notes:
        WHAT NEEDS THEM COMES FIRST, and money comes before stock. An earlier
        version opened with a catalogue count, which answers a question nobody
        asks on arrival: a seller opening this thread wants to know whether
        anybody owes them money. Everything else is housekeeping.

        THE STATUS LINE NEVER SAYS "LIVE". It used to read "Closed — buyers
        can't see it yet" directly above "8 live", which is true in the database
        and nonsense to a person: one line says nobody can see the shop and the
        next says eight things are live. One word per idea — a shop is open or
        closed, an item is in the shop or needs a price.

        IT ALWAYS ENDS WITH A QUESTION AND SOMETHING TO TAP. A chat has no menu
        bar; the last message on screen is the entire interface, so a reply that
        states a fact and stops is a dead end. There is always at least the
        share link, because a seller with nothing outstanding still wants the
        thing they came for.
    """
    unpriced = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.DRAFT.value,
                Product.price_kes.is_(None),
            )
        )
        or 0
    )
    ready = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.DRAFT.value,
                Product.price_kes.is_not(None),
            )
        )
        or 0
    )
    in_shop = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.PUBLISHED.value,
            )
        )
        or 0
    )
    waiting = (
        db.scalar(
            select(func.count(Order.id)).where(
                Order.seller_id == seller.id,
                Order.status == OrderStatus.AWAITING_CONFIRMATION.value,
            )
        )
        or 0
    )
    has_payment = get_payment_method(db, seller) is not None

    lines = [f"*{seller.display_name}*"]
    if seller.is_published:
        lines.append(f"Open · {_plural(in_shop, 'item')} for sale")
    elif in_shop:
        lines.append(f"Closed · {_plural(in_shop, 'item')} ready to sell")
    else:
        lines.append("Closed · nothing in it yet")

    # Ordered by what costs the seller most to ignore. Money first.
    todo: list[str] = []
    if waiting:
        todo.append(f"💰 {_plural(waiting, 'order')} waiting for you to confirm payment")
    if unpriced:
        todo.append(f"🏷️ {_plural(unpriced, 'item')} {'needs' if unpriced == 1 else 'need'} a price")
    if not has_payment:
        todo.append("💳 Buyers have no way to pay you yet")
    if ready:
        todo.append(f"📦 {_plural(ready, 'item')} ready to go in your shop")

    if todo:
        lines.append("")
        lines.extend(todo)

    lines.append("")
    lines.append("What do you need?")

    # At most three, in the same order of consequence. Everything else stays
    # reachable by typing — the buttons are the shortcut, never the only door.
    candidates: list[tuple[str, str]] = []
    if waiting:
        candidates.append(("orders", f"Orders ({waiting})"))
    if unpriced:
        candidates.append(("prices", "Add prices"))
    if not has_payment:
        candidates.append(("payments", "Get paid"))
    if ready:
        candidates.append(("publish", "Add to my shop"))
    if in_shop and not seller.is_published:
        candidates.append(("open", "Open for business"))
    candidates.append(("share", "My shop link"))

    return [Reply("\n".join(lines), buttons=candidates[:3])]


def handle(
    db: Session,
    phone: str,
    text: str,
    media: list[tuple[str, MediaFetch]] | None = None,
) -> Outcome:
    """
    Advance one buyer's conversation by one message.

    Args:
        db: Session. The caller commits — this function never does, so a failed
            reply cannot leave a half-applied order behind.
        phone: Bare digits with country code.
        text: What they sent, which is the caption when media is attached.
        media: ``(media_id, fetch)`` per attachment. The fetch is a callable
            rather than a URL because the two providers differ: Twilio serves a
            URL behind basic auth, Meta an id resolved through two Graph calls.
            Which one delivered this message is the webhook's business, not the
            conversation's.

    Returns:
        An :class:`Outcome` carrying the replies to send back, in order.

    Notes:
        THE GLOBAL WORDS ARE CHECKED BEFORE STATE. "menu", "cart" and "help"
        work from anywhere, because a buyer who is lost cannot be required to
        first find the screen where escaping is offered.
    """
    convo = get_conversation(db, phone)
    said = text.strip()
    lowered = said.lower()

    # ── A seller forwarding their catalogue ─────────────────────────────────
    # Checked before anything else: this is the one interaction the whole
    # product exists for, and a photo from a seller can mean nothing else.
    if media:
        owner = find_seller_by_phone(db, phone)
        if owner is not None:
            replies: list[Reply] = []
            needs_price: list[int] = []
            for media_id, fetch in media:
                said_replies, product_id = _handle_forward(db, owner, media_id, fetch, said)
                replies.extend(said_replies)
                if product_id is not None:
                    needs_price.append(product_id)

            if needs_price:
                # Queue them and ask about the first BY NAME. Asking "what are
                # the prices" after eight photos makes the seller reconstruct an
                # order we never showed them.
                convo.state = ConversationState.PRICING
                convo.context = {"pricing_queue": needs_price}
                replies.extend(_pricing_prompt(db, owner, convo))
            return Outcome(replies)

    # ── Routing: which shop is this? ────────────────────────────────────────
    # The shareable link is wa.me/<bot>?text=shop%20<slug>, so the buyer's very
    # first message names the shop. That link opens WhatsApp itself — it is a
    # deep link, not a URL, so nothing can redirect it to a browser.
    if lowered.startswith("shop "):
        wanted = said[5:].strip()
        seller = _find_shop(db, wanted)
        if seller is None:
            return Outcome(
                [
                    Reply(
                        f"I can't find a shop called *{wanted}*.\n\n"
                        "Check the link the seller sent you, or ask them for it again."
                    )
                ]
            )
        convo.seller_id = seller.id

        # A SELLER WHO OPENED SOMEBODY ELSE'S LINK IS A BUYER NOW, and is told
        # so rather than left to discover it. One number is not one role: the
        # bot number is shared, and the same person runs a shop on Monday and
        # buys shoes on Tuesday. Announcing the switch — and naming the way
        # back — is what makes a shared number honest instead of confusing.
        owner_here = find_seller_by_phone(db, phone)
        switched = (
            [
                Reply(
                    f"You're shopping at *{seller.display_name}* now.\n\n"
                    f"_Send *my shop* whenever you want to get back to running "
                    f"{owner_here.display_name}._"
                )
            ]
            if owner_here is not None and owner_here.id != seller.id
            else []
        )

        # ARRIVAL, so the storefront is offered ALONGSIDE the menu rather than
        # instead of it. The chat can sell; a page browses forty items better
        # than a list picker ever will, and the buyer should not have to pick
        # one surface before seeing either.
        return Outcome([*switched, *_menu(db, seller, convo, arriving=True), _shop_card(seller)])

    # ── The seller's own side of the thread ─────────────────────────────────
    # Checked before anything else a buyer could mean. A number typed by a
    # seller we just asked for a price is a price, not a menu choice.
    owner = find_seller_by_phone(db, phone)
    if owner is not None:
        # THE WAY BACK OUT OF SOMEBODY ELSE'S SHOP. Checked before the buyer
        # branch claims the message, because a seller stuck inside another
        # shop's conversation with no exit is worse than never having let them
        # browse at all.
        if lowered in {"my shop", "back to my shop", "my store"}:
            convo.seller_id = None
            convo.state = ConversationState.NEW
            convo.context = {}
            return Outcome(_seller_home(db, owner))

        if convo.state == ConversationState.PRICING:
            if lowered in {"skip", "later"}:
                queue = [int(pid) for pid in convo.context.get("pricing_queue", [])]
                convo.context = {**convo.context, "pricing_queue": queue[1:]}
                return Outcome(_pricing_prompt(db, owner, convo))
            if lowered in {"stop", "done", "cancel"}:
                convo.state = ConversationState.NEW
                convo.context = {}
                return Outcome(_priced_summary(db, owner))
            amount = _price(said)
            if amount is not None:
                return Outcome(_set_price(db, owner, convo, amount))
            return Outcome([Reply("I need just the number — like *1800*. Or send *skip*.")])

        if convo.state == ConversationState.PAY_KIND:
            kind = _PAY_ALIASES.get(lowered.removeprefix("pay:").strip())
            if kind is not None:
                return Outcome(_ask_payment_number(convo, kind))
            return Outcome([Reply("Pochi, till or paybill — which of the three is it?")])

        if convo.state == ConversationState.PAY_NUMBER:
            if lowered in {"cancel", "stop", "later"}:
                convo.state = ConversationState.NEW
                convo.context = {}
                return Outcome(_seller_home(db, owner))
            return Outcome(_save_payment(db, owner, convo, said))

        if lowered in {"orders", "my orders", "sales"}:
            return Outcome(_seller_orders(db, owner))
        if lowered.startswith("confirm:"):
            return Outcome(_confirm_order(db, owner, said.split(":", 1)[1].strip()))
        if lowered in {"payments", "payment", "get paid", "set that up"}:
            return Outcome(_ask_payment_kind(convo))
        if lowered in {"share", "my shop link", "link", "my link"}:
            return Outcome([_share_card(owner)])
        if lowered in {"prices", "add prices", "price"}:
            return Outcome(_resume_pricing(db, owner, convo))
        if lowered in {"publish", "add to my shop"}:
            return Outcome(_publish_ready(db, owner))
        if lowered in {"open", "open shop", "go live", "open for business"}:
            return Outcome(_open_shop(db, owner))
        if lowered in {"drafts", "stock", "my products", "items"}:
            return Outcome(_priced_summary(db, owner))
        if lowered in {"store", "shop", "my shop", "website"}:
            # Reachable for a SELLER too. The buyer branch below only fires
            # once somebody has opened a shop LINK, so without this a seller
            # asking for their own store fell through to the signup question.
            return Outcome([_shop_card(owner)])

        # ANYTHING ELSE FROM A SELLER IS ANSWERED WITH THEIR OWN HOME, and that
        # is a guardrail rather than a nicety. Without it a seller who typed
        # something we could not read fell through to the stranger's question —
        # "are you here to sell, or to shop?" — asked of somebody whose shop we
        # are already running. It read as the system forgetting them.
        #
        # The one exception is a seller browsing SOMEBODY ELSE'S shop. They
        # opened a link like any buyer, and the buyer branch below is theirs
        # for as long as the conversation points at that shop.
        browsing_elsewhere = convo.seller_id is not None and convo.seller_id != owner.id
        if not browsing_elsewhere:
            return Outcome(_seller_home(db, owner))

    # ── Somebody with no shop, who has not opened one either ────────────────
    if convo.state == ConversationState.NAMING:
        return Outcome(_create_shop(db, convo, phone, said))

    seller = convo.seller
    if seller is None or not seller.is_published:
        if lowered in {"sell", "i want to sell", "sella"}:
            return Outcome(_ask_shop_name(convo))
        if lowered in {"buy", "i'm shopping", "im shopping", "shopping"}:
            return Outcome(
                [
                    Reply(
                        "Open the shop's link and I'll show you what they have.\n\n"
                        "_It looks like wa.me/…?text=shop theirshop — ask the "
                        "seller for theirs._"
                    )
                ]
            )
        # The bot number serves both sides, and a stranger's first message
        # cannot tell us which they are. Guessing wrong is expensive both ways:
        # a buyer walked through shop setup abandons, and a seller told to
        # "open a shop's link" has been handed a riddle.
        return Outcome(_welcome(convo))

    # ── A tap on a button or a list row ─────────────────────────────────────
    # These arrive as the ID we set when sending, so they are unambiguous in a
    # way a typed word never is: "cat:Shoes" cannot also mean a product name or
    # a menu position. Typed words still work — the ids are an addition, not a
    # replacement, because a buyer on a client that draws no buttons must not
    # be stranded.
    if lowered.startswith("cat:"):
        return Outcome(_list_products(db, seller, convo, category=said[4:]))

    if lowered.startswith("prod:"):
        product = db.get(Product, int(said[5:])) if said[5:].isdigit() else None
        # Scoped to this shop: a product id is guessable, and one shop's chat
        # must never render another shop's stock.
        if product is not None and product.seller_id == seller.id:
            return Outcome(_show_product(convo, product))
        return Outcome(_menu(db, seller, convo))

    # ── Words that work from anywhere ───────────────────────────────────────
    if lowered in {"menu", "hi", "hello", "start", "habari", "niaje", "keep shopping"}:
        return Outcome(_menu(db, seller, convo))

    if lowered in {"store", "shop", "website", "open store", "see everything"}:
        # The only reply that can open a page inside WhatsApp — see _shop_card.
        return Outcome([_shop_card(seller)])

    if lowered in {"cart", "my basket", "basket"}:
        return Outcome(_show_cart(db, seller, convo))

    if lowered in {"clear", "empty basket"}:
        clear(db, _basket(db, convo, seller))
        convo.state = ConversationState.BROWSING
        convo.context = {}
        return Outcome([Reply("Basket emptied.", buttons=[("menu", "Start again")])])

    # SPELLED BOTH WAYS ON PURPOSE. A buyer typed "Check out" and got the shop
    # menu, because the match was `== "checkout"` and a space is not nothing.
    # The button sends the id, so this only ever bites somebody typing — which
    # is exactly the buyer least able to recover from it.
    if lowered in {"checkout", "check out", "pay", "order"}:
        return Outcome(_start_checkout(db, seller, convo))

    if lowered == "help":
        return Outcome(
            [
                Reply(
                    f"I'm here to help you buy from *{seller.display_name}*.\n\n"
                    "Tap the buttons, or send a word:\n"
                    "*menu* — see what's on sale\n"
                    "*cart* — your basket\n"
                    "*checkout* — place your order\n"
                    "*clear* — empty the basket",
                    buttons=[
                        ("menu", "See what's on sale"),
                        ("cart", "My basket"),
                        ("checkout", "Checkout"),
                    ],
                )
            ]
        )

    # ── Answers that only mean something in a particular state ──────────────
    if convo.state == ConversationState.BROWSING:
        chosen = _digits(said)
        options = convo.context.get("options", [])
        if chosen and 1 <= chosen <= len(options):
            return Outcome(_list_products(db, seller, convo, category=options[chosen - 1]))

    elif convo.state == ConversationState.LISTING:
        chosen = _digits(said)
        ids = convo.context.get("products", [])
        if chosen and 1 <= chosen <= len(ids):
            product = db.get(Product, ids[chosen - 1])
            if product is not None:
                return Outcome(_show_product(convo, product))

    elif convo.state == ConversationState.PRODUCT:
        if lowered in {"add", "add to cart", "add to basket", "buy"}:
            product_id = convo.context.get("product")
            product = db.get(Product, product_id) if isinstance(product_id, int) else None
            if product is None or product.seller_id != seller.id:
                return Outcome(_menu(db, seller, convo))

            # CHECKED HERE, because the cart service deliberately does not.
            # `_purchasable` asks whether a buyer may put this shop's product in
            # a basket at all; having run out is a different question, answered
            # again at checkout by `unavailable_lines` for the web flow.
            #
            # The card already says "sold out" and offers no Add button — but a
            # typed "add" went straight past it, and the buyer only found out at
            # checkout that their basket could not be ordered.
            if product.stock <= 0:
                return Outcome(
                    [
                        Reply(
                            f"*{product.title}* is sold out just now.",
                            buttons=[("menu", "See what's in stock")],
                        )
                    ]
                )

            # THE SIZE IS ASKED BEFORE THE BASKET, because afterwards the seller
            # is the one who has to go and ask.
            if product.sizes:
                return Outcome(_ask_variant(convo, product))
            return Outcome(_add_to_basket(db, seller, convo, product))

    elif convo.state == ConversationState.VARIANT:
        product_id = convo.context.get("product")
        sizes = [str(s) for s in convo.context.get("sizes", [])]
        product = db.get(Product, product_id) if isinstance(product_id, int) else None
        if product is None or product.seller_id != seller.id:
            return Outcome(_menu(db, seller, convo))

        # Named apart from the numeric `chosen` above: that one is a menu
        # position and this one is a size, and mypy caught them sharing a name.
        wanted = said.removeprefix("size:").strip() if lowered.startswith("size:") else said.strip()
        # Matched case-insensitively against what we actually offered: a typed
        # "40" has to mean the same as the row that says Size 40, and a size we
        # never listed must not reach the order as though the seller stocked it.
        picked = next((s for s in sizes if s.lower() == wanted.lower()), None)
        if picked is None:
            return Outcome(_ask_variant(convo, product))
        return Outcome(_add_to_basket(db, seller, convo, product, variant=picked))

    elif convo.state == ConversationState.CHECKOUT_NAME:
        name = said.strip()
        if len(name) < 2 or len(name) > 120:
            return Outcome([Reply("Sorry — what name should I put on the order?")])
        return Outcome(_ask_delivery(convo, name))

    elif convo.state == ConversationState.CHECKOUT_DELIVERY:
        name = str(convo.context.get("buyer_name", ""))
        if lowered in {"collect", "i'll collect", "ill collect", "pickup", "pick up"}:
            return Outcome(_place(db, seller, convo, phone, name=name, address=None))
        if lowered in {"deliver", "deliver to me", "delivery"}:
            return Outcome(_ask_address(convo))
        return Outcome(_ask_delivery(convo, name))

    elif convo.state == ConversationState.ADDRESS:
        address = said.strip()
        if len(address) < 3:
            return Outcome(_ask_address(convo))
        return Outcome(
            _place(
                db,
                seller,
                convo,
                phone,
                name=str(convo.context.get("buyer_name", "")),
                address=address,
            )
        )

    elif convo.state == ConversationState.PAYING:
        return Outcome(_claim(db, convo, said))

    # ── Anything we could not read ──────────────────────────────────────────
    # Re-offering the menu beats "I didn't understand": the buyer's problem is
    # not that we failed to parse, it is that they cannot see their options.
    #
    # NOT GREETED AGAIN, THOUGH. _menu only introduces the shop on arrival, so
    # landing here mid-purchase no longer reads as being met at the door by
    # somebody who has forgotten the last ten minutes.
    return Outcome(_menu(db, seller, convo))
