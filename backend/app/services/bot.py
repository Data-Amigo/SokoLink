"""
The shop, as a conversation. No browser anywhere in it.

    inbound text ──▶ handle() ──▶ [Reply, ...] ──▶ TwiML in the webhook response

WHY THE CHAT IS THE SHOP. A web link is the one thing we cannot control: tapping
a URL hands an intent to the operating system, and some Android skins route it
to the default browser however the message was built. Tested on a real handset —
plain link and native CTA button both left WhatsApp. So the buyer surface stops
being a page we link to and becomes the thread itself, which no OS can redirect.

NUMBERED TEXT MENUS, NOT NATIVE LIST WIDGETS. A list picker is prettier and
needs a pre-approved content template per shape, sent through the REST API. A
numbered menu is plain text in the webhook's own response: it works on the
sandbox today, needs no approval, and degrades to something readable on any
client. The state machine below does not care which renders it, so widgets can
replace the text later without touching this logic.

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

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Cart,
    ConversationState,
    Order,
    PriceSource,
    Product,
    ProductStatus,
    Seller,
    WaConversation,
)
from app.services.cart import CartError, add_item, clear, get_or_create_cart
from app.services.catalogue import PublishError, publish_product
from app.services.intake import IntakeError, MediaFetch, ingest_forwarded_post
from app.services.media import absolute_url
from app.services.orders import claim_payment, get_payment_method, place_order
from app.services.storefront import get_categories, get_public_products

#: How many numbered options one message may offer. Past this a buyer is
#: scrolling a wall of text on a phone and stops reading; categories exist
#: precisely so a catalogue never has to be shown flat.
PAGE_SIZE = 8


@dataclass(frozen=True)
class Reply:
    """
    One outbound WhatsApp message.

    Args:
        body: The text.
        media_url: An absolute image URL, or None. Relative paths cannot work —
            Twilio fetches this from its own servers, not from a browser with
            our origin.
    """

    body: str
    media_url: str | None = None


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


def _greeting(seller: Seller) -> str:
    return f"*{seller.display_name}*\nKaribu! 👋"


def _menu(db: Session, seller: Seller, convo: WaConversation) -> list[Reply]:
    """
    The category menu, and the state that makes its numbers meaningful.

    A shop with no categories skips straight to the product list rather than
    showing an empty menu — a seller who never typed a category should not cost
    their buyer a dead screen.
    """
    categories = get_categories(db, seller)[:PAGE_SIZE]
    if not categories:
        return _list_products(db, seller, convo, category=None)

    convo.state = ConversationState.BROWSING
    convo.context = {"options": categories}

    lines = [f"{i}. {name}" for i, name in enumerate(categories, start=1)]
    body = (
        f"{_greeting(seller)}\n\nWhat are you looking for?\n\n"
        + "\n".join(lines)
        + "\n\nReply with a number, or send *cart* to see your basket."
    )
    return [Reply(body)]


def _list_products(
    db: Session, seller: Seller, convo: WaConversation, category: str | None
) -> list[Reply]:
    """A numbered page of products, and the state that decodes the reply."""
    products = get_public_products(db, seller, category=category)[:PAGE_SIZE]

    if not products:
        convo.state = ConversationState.BROWSING
        convo.context = {}
        return [Reply("Nothing in stock there yet. Send *menu* to look at something else.")]

    convo.state = ConversationState.LISTING
    convo.context = {"products": [p.id for p in products], "category": category}

    lines = []
    for i, product in enumerate(products, start=1):
        price = product.price_display or "Ask for price"
        sold_out = " — *sold out*" if product.stock <= 0 else ""
        lines.append(f"{i}. {product.title} — {price}{sold_out}")

    heading = category or "Everything"
    body = (
        f"*{heading}*\n\n"
        + "\n".join(lines)
        + "\n\nReply with a number to see it, or *menu* to go back."
    )
    return [Reply(body)]


def _show_product(convo: WaConversation, product: Product) -> list[Reply]:
    """One product, with its photo, and the two things a buyer can do next."""
    convo.state = ConversationState.PRODUCT
    convo.context = {"product": product.id}

    parts = [f"*{product.title}*"]
    if product.price_display:
        parts.append(product.price_display)
    if product.description:
        parts.append(product.description)
    if product.sizes:
        parts.append("Sizes: " + ", ".join(product.sizes))

    if product.stock <= 0:
        parts.append("\n_Sold out right now._\nSend *menu* to see something else.")
    else:
        parts.append("\nSend *add* to put this in your basket, or *menu* to keep looking.")

    return [Reply("\n".join(parts), media_url=absolute_url(product.cover_url))]


def _show_cart(db: Session, seller: Seller, convo: WaConversation) -> list[Reply]:
    """The basket, and the decision it exists to prompt."""
    cart = _basket(db, convo, seller)

    if not cart.items:
        convo.state = ConversationState.BROWSING
        convo.context = {}
        return [Reply("Your basket is empty. Send *menu* to start shopping.")]

    convo.state = ConversationState.CART
    convo.context = {}

    lines = [
        # The cart reads through to the live product on purpose. Only an ORDER
        # line copies the name and price, so a rename mid-basket is fine but a
        # rename after checkout can never rewrite what somebody paid.
        f"• {item.product.title} × {item.quantity} — {_money(item.line_total_kes)}"
        for item in cart.items
    ]
    body = (
        "*Your basket*\n\n"
        + "\n".join(lines)
        + f"\n\nTotal: *{_money(cart.subtotal_kes)}*"
        + "\n\nSend *checkout* to order, *menu* to keep shopping, or *clear* to empty it."
    )
    return [Reply(body)]


def _start_checkout(db: Session, seller: Seller, convo: WaConversation) -> list[Reply]:
    """
    Ask for the one thing an order needs that a chat does not already know.

    WhatsApp gives us the phone, which is both the M-Pesa line and the way to
    reach them. It does not give us a name or an address, and asking for both in
    one message costs one round trip instead of two — which matters on a slow
    connection far more than tidy parsing does.
    """
    cart = _basket(db, convo, seller)
    if not cart.items:
        return _show_cart(db, seller, convo)

    if get_payment_method(db, seller) is None:
        # Nowhere for the money to go. Say so plainly rather than taking an
        # order the seller cannot be paid for.
        return [
            Reply(
                "This shop hasn't set up payments yet, so I can't take the order. "
                "Send *menu* to keep looking."
            )
        ]

    convo.state = ConversationState.ADDRESS
    convo.context = {}
    return [
        Reply(
            "Almost done. Reply with your *name and where to deliver*, "
            "separated by a comma.\n\nFor example: _Akinyi Otieno, Kasarani_"
        )
    ]


def _place(
    db: Session, seller: Seller, convo: WaConversation, phone: str, text: str
) -> list[Reply]:
    """Turn the basket into an order, and tell them exactly how to pay."""
    name, _, address = text.partition(",")
    name, address = name.strip(), address.strip()
    if not name or not address:
        return [
            Reply(
                "I need both, separated by a comma — your name, then where to deliver.\n\n"
                "For example: _Akinyi Otieno, Kasarani_"
            )
        ]

    cart = _basket(db, convo, seller)
    if not cart.items:
        return _show_cart(db, seller, convo)

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
    db.flush()

    convo.state = ConversationState.PAYING
    convo.order_reference = order.reference
    convo.context = {}

    to_name = f" ({order.paid_to_name})" if order.paid_to_name else ""
    return [
        Reply(
            f"*Order {order.reference}*\n"
            f"Total: *{_money(order.total_kes)}*\n\n"
            f"Send the money to *{order.paid_to_number}*{to_name} on M-Pesa, "
            f"then reply here with the M-Pesa code.\n\n"
            f"_{seller.display_name} confirms it once the money shows up._"
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


def _publish_ready(db: Session, seller: Seller) -> list[Reply]:
    """
    Publish every priced draft, and say what a buyer can now reach.

    Notes:
        IT GOES THROUGH THE SAME GATE the workspace uses. publish_product
        refuses an unpriced item and names it, so the chat cannot become a
        second way to go live that skips the rule.

        THE SHOP ITSELF IS NOT OPENED HERE. Publishing an item and opening a
        storefront are different decisions, and a seller who has not seen their
        shop should not discover it is live because they priced a shirt.
    """
    drafts = db.scalars(
        select(Product).where(
            Product.seller_id == seller.id,
            Product.status == ProductStatus.DRAFT.value,
            Product.price_kes.is_not(None),
        )
    ).all()

    if not drafts:
        return [Reply("Nothing is priced yet. Send me a photo of what you're selling.")]

    published = []
    for product in drafts:
        try:
            publish_product(db, seller, product.id)
            published.append(product.title)
        except PublishError as exc:
            return [Reply(str(exc))]

    names = "\n".join(f"• {title}" for title in published[:PAGE_SIZE])
    if seller.is_published:
        return [
            Reply(f"Live now:\n\n{names}\n\nYour shop: {settings.app_base_url}/shop/{seller.slug}")
        ]

    return [
        Reply(
            f"Published:\n\n{names}\n\n"
            f"Your shop is still closed, so nobody can see them yet. "
            f"Send *open* to open it."
        )
    ]


def _open_shop(db: Session, seller: Seller) -> list[Reply]:
    """
    Open the storefront, if it is allowed to open.

    Notes:
        THE DATABASE ALSO REFUSES a published shop with no WhatsApp number — a
        live shop nobody can contact is a dead end. That rail cannot fire on
        this path, though, and it is worth saying why: a seller is RECOGNISED
        by their WhatsApp number, so one without a number never reaches here as
        a seller at all. The guard lives in the workspace, where a shop can be
        opened by someone signed in with an email.
    """
    live = db.scalar(
        select(func.count(Product.id)).where(
            Product.seller_id == seller.id,
            Product.status == ProductStatus.PUBLISHED.value,
        )
    )
    if not live:
        return [Reply("Publish something first, then I can open your shop.")]

    seller.is_published = True
    db.flush()
    return [
        Reply(
            f"Your shop is open. 🎉\n\n{settings.app_base_url}/shop/{seller.slug}\n\n"
            f"Share that link anywhere — WhatsApp, TikTok, your status."
        )
    ]


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
        slug = lowered.removeprefix("shop ").strip()
        seller = db.scalar(select(Seller).where(Seller.slug == slug, Seller.is_published.is_(True)))
        if seller is None:
            return Outcome([Reply("I can't find that shop. Check the link and try again.")])
        convo.seller_id = seller.id
        return Outcome(_menu(db, seller, convo))

    # ── The seller's own side of the thread ─────────────────────────────────
    # Checked before anything else a buyer could mean. A number typed by a
    # seller we just asked for a price is a price, not a menu choice.
    owner = find_seller_by_phone(db, phone)
    if owner is not None:
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

        if lowered == "publish":
            return Outcome(_publish_ready(db, owner))
        if lowered in {"open", "open shop", "go live"}:
            return Outcome(_open_shop(db, owner))
        if lowered in {"drafts", "stock", "my products"}:
            return Outcome(_priced_summary(db, owner))

    seller = convo.seller
    if seller is None or not seller.is_published:
        return Outcome(
            [
                Reply(
                    "Karibu! 👋 I'm Biashara Mall.\n\n"
                    "Open a shop's link to start — it looks like "
                    "_wa.me/…?text=shop yourshop_."
                )
            ]
        )

    # ── Words that work from anywhere ───────────────────────────────────────
    if lowered in {"menu", "hi", "hello", "start", "habari", "niaje"}:
        return Outcome(_menu(db, seller, convo))

    if lowered == "cart":
        return Outcome(_show_cart(db, seller, convo))

    if lowered == "clear":
        clear(db, _basket(db, convo, seller))
        convo.state = ConversationState.BROWSING
        return Outcome([Reply("Basket emptied. Send *menu* to start again.")])

    if lowered == "checkout":
        return Outcome(_start_checkout(db, seller, convo))

    if lowered == "help":
        return Outcome(
            [
                Reply(
                    "*menu* — see what's on sale\n"
                    "*cart* — your basket\n"
                    "*checkout* — place your order\n"
                    "*clear* — empty the basket"
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
        if lowered in {"add", "add to cart", "buy"}:
            product_id = convo.context.get("product")
            if not isinstance(product_id, int):
                return Outcome(_menu(db, seller, convo))
            try:
                cart = _basket(db, convo, seller)
                add_item(db, cart, product_id, quantity=1)
            except CartError:
                # Sold out, or gone from the catalogue since we showed it.
                return Outcome([Reply("That's just sold out. Send *menu* to see what's left.")])
            return Outcome(
                [Reply("Added. ✅ Send *cart* to check out, or *menu* to keep shopping.")]
                + _show_cart(db, seller, convo)[1:]
            )

    elif convo.state == ConversationState.ADDRESS:
        return Outcome(_place(db, seller, convo, phone, said))

    elif convo.state == ConversationState.PAYING:
        return Outcome(_claim(db, convo, said))

    # ── Anything we could not read ──────────────────────────────────────────
    # Re-offering the menu beats "I didn't understand": the buyer's problem is
    # not that we failed to parse, it is that they cannot see their options.
    return Outcome(_menu(db, seller, convo))
