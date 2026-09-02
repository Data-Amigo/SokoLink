"""
The reply builders: menus, cards, product views, prompts.
"""

from __future__ import annotations

from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ConversationState,
    Product,
    ProductStatus,
    Seller,
    WaConversation,
)
from app.services.bot.common import _basket
from app.services.bot.replies import (
    _ASKING_FOR,
    PAGE_SIZE,
    VARIANT_LIMIT,
    Outcome,
    Reply,
    _money,
    _plural,
)
from app.services.cart import CartError, add_item
from app.services.media import absolute_url
from app.services.orders import (
    get_payment_method,
)
from app.services.storefront import get_categories, get_public_products


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


_CATEGORY_EMOJI: tuple[tuple[str, str], ...] = (
    ("book", "📚"),
    ("revision", "📝"),
    ("stationer", "✏️"),
    ("school", "🎓"),
    ("shoe", "👟"),
    ("sandal", "👡"),
    ("bag", "👜"),
    ("cloth", "👕"),
    ("fashion", "👗"),
    ("dress", "👗"),
    ("beauty", "💄"),
    ("hair", "💇"),
    ("phone", "📱"),
    ("electronic", "🔌"),
    ("kitchen", "🍳"),
    ("home", "🏠"),
    ("food", "🍲"),
    ("baby", "🧸"),
    ("jewel", "💍"),
    ("watch", "⌚"),
)


def _category_icon(name: str) -> str:
    """A picture for one category, or a bullet when nothing fits."""
    lowered = name.lower()
    for keyword, emoji in _CATEGORY_EMOJI:
        if keyword in lowered:
            return emoji
    return "•"


def _shop_blurb(db: Session, seller: Seller) -> str:
    """
    One line saying what this shop actually sells.

    Notes:
        THE SELLER'S OWN WORDS FIRST. ``bio`` is what they wrote about
        themselves, and nothing we generate will describe their shop better than
        they can. `about` in the chat is how they set it.

        DERIVED FROM CATEGORIES OTHERWISE, because the alternative is a welcome
        that names a shop and says nothing about it — which is the difference
        between "Karibu Vitabu Bora" and "Karibu Vitabu Bora, we sell books and
        revision materials". The categories are the seller's own words too; they
        typed them onto their products.

        EMPTY WHEN WE KNOW NOTHING. A sentence invented to fill the gap would be
        us describing somebody's business to their own customers on no evidence.
    """
    if seller.bio and seller.bio.strip():
        return seller.bio.strip()

    categories = get_categories(db, seller)
    if not categories:
        return ""

    lowered = [c.lower() for c in categories]
    listed = lowered[0] if len(lowered) == 1 else ", ".join(lowered[:-1]) + f" and {lowered[-1]}"
    return f"{seller.display_name} sells {listed}, straight here on WhatsApp."


def _greeting(db: Session, seller: Seller) -> list[Reply]:
    """
    The shop's welcome: who it is, what it sells, what it has, how to ask.

    Notes:
        THIS IS THE SHOP FRONT. A buyer arrives from a status post into a thread
        whose header reads "Biasharamall", because the number is shared — so
        everything they learn about whose shop this is, they learn here. Two
        earlier versions got it wrong in opposite directions: one repeated
        "Karibu!" on every single message, the other answered "Hi" with a bare
        numbered list belonging to nobody.

        IT TEACHES THE INTERFACE BY EXAMPLE. The buyer can type what they want
        in their own words — but nothing told them so, and a person handed a
        numbered menu assumes numbers are all it takes. The examples are the
        cheapest way to say "talk to me normally", and each one is a query that
        genuinely works: a name, a description, a budget.

        NO EXAMPLE PROMISES SOMETHING WE CANNOT DO. That rule cost a feature —
        "what do you have under KSh 1,000" was written first and the price
        filter built afterwards, rather than shipping a suggestion that would
        have dropped the buyer into a fallback.
    """
    count = (
        db.scalar(
            select(func.count(Product.id)).where(
                Product.seller_id == seller.id,
                Product.status == ProductStatus.PUBLISHED.value,
            )
        )
        or 0
    )

    parts = [f"Karibu {seller.display_name}! 👋"]

    blurb = _shop_blurb(db, seller)
    if blurb:
        parts.append(blurb)

    if count:
        parts.append(f"We have *{_plural(count, 'item')}* available right now.")

    parts.append(
        "Tell me what you're looking for and I'll find it — you can say things like:\n\n"
        '_"Show me school bags"_\n'
        '_"I\'m looking for a revision book"_\n'
        '_"What do you have under 1000?"_'
    )

    categories = get_categories(db, seller)[:PAGE_SIZE]
    if categories:
        # NUMBERED AS WELL AS PICTURED. The list picker is the nice path,
        # and the typed number is the one that works on a handset that
        # draws no picker — which is a lot of them here. Dropping the
        # numbers left BROWSING still mapping "2" to a category with
        # nothing on screen saying so.
        listed = "\n".join(
            f"{i}. {_category_icon(name)}  {name}" for i, name in enumerate(categories, start=1)
        )
        parts.append(f"Or browse what we have:\n\n{listed}")

    parts.append("What are you after today? 😊")

    return [
        Reply(
            "\n\n".join(parts),
            rows=[(f"cat:{name}", name, "") for name in categories] or None,
            list_label="Browse",
        )
    ]


def _menu(
    db: Session, seller: Seller, convo: WaConversation, *, greet: bool = False
) -> list[Reply]:
    """
    The category menu, and the state that makes its numbers meaningful.

    Args:
        db: Session.
        seller: The shop.
        convo: The conversation, moved into BROWSING.
        greet: Whether to introduce the shop before asking anything.

    Notes:
        A GREETING IS ANSWERED WITH A GREETING. Somebody who types "Hi" is
        saying hello, and replying with a bare "What are you looking for?" is
        the rudest thing in the flow — it answers a person with a form. Somebody
        who taps "Keep shopping" mid-basket is not saying hello, and being
        introduced to the shop again is the opposite failure.

        So the CALLER decides, from what was actually said, rather than this
        function guessing from state. Greeting words and arrival greet; menu
        taps, fallbacks and mid-flow returns do not.

        BOTH MISTAKES HAVE NOW BEEN MADE HERE, in that order. First it greeted
        every single time, so an unreadable word mid-purchase met the buyer at
        the door as though they had just walked in. Then it greeted almost
        never, and somebody opening with "Hi" was answered by a numbered list
        belonging to nobody.

        A shop with no categories skips straight to the product list rather than
        showing an empty menu — a seller who never typed a category should not
        cost their buyer a dead screen.
    """
    categories = get_categories(db, seller)[:PAGE_SIZE]
    if not categories:
        return _greet_then(db, seller, convo, greet=greet)

    convo.state = ConversationState.BROWSING
    convo.context = {"options": categories}

    # The body still lists every option. Meta draws the rows as a picker, but a
    # buyer on a cheap handset replying "2" is a real pattern here, and the
    # numbers are what make that work.
    lines = [f"{i}. {name}" for i, name in enumerate(categories, start=1)]
    # THE WELCOME IS A WHOLE MESSAGE, not a prefix. It names the shop, says
    # what it sells, counts the stock and teaches the buyer they can simply
    # ask — a numbered list bolted under a greeting does none of that.
    if greet:
        return _greeting(db, seller)

    return [
        Reply(
            "What are you looking for?\n\n" + "\n".join(lines),
            rows=[(f"cat:{name}", name, "") for name in categories],
            list_label="Browse",
        )
    ]


def _greet_then(
    db: Session, seller: Seller, convo: WaConversation, *, greet: bool = False
) -> list[Reply]:
    """Everything in the shop — for a shop whose seller never typed a category."""
    replies = _list_products(db, seller, convo, category=None)
    if not greet:
        return replies

    # Welcome first, then the whole catalogue — two messages, because the
    # welcome has its own job and a shop with no categories still deserves one.
    return [*_greeting(db, seller), *replies]


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


def _still_reading() -> list[Reply]:
    """
    A seller wrote something while their forwarded photos were still being read.

    Consulting the model here produced the shrug in the screenshots — "I'm not
    quite sure what you're referring to" — because the model had no idea a batch
    was mid-flight. The honest answer is that it is, and the summary is coming.
    """
    return [
        Reply(
            "Still reading your photos — I'll list them all here in a moment and "
            "ask you for any prices I couldn't see. \U0001f4f8"
        )
    ]


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
    # shop:<slug>, not the display name. The slug is ours, permanent and
    # unambiguous — "shop:book-lounge" resolves to exactly one shop, where
    # "Shop Book Lounge" could collide with a product name a buyer types. The
    # colon stays literal (safe=":") so the link a seller pastes reads clean.
    return f"https://wa.me/{number}?text={quote(f'shop:{seller.slug}', safe=':')}"


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


_MISSING: dict[str, str] = {
    "shop_name": "Sure — what should it be called instead?",
    "about": ("Go on then — tell buyers what your shop sells, in a sentence."),
    "query": "What are you looking for?",
    "budget": "What are you looking to spend?",
}


def _ask_for(convo: WaConversation, what: str) -> Outcome:
    """
    Ask for the one thing that was missing, and remember we asked.

    Notes:
        THE HOLE THIS CLOSES. The model can understand a request perfectly and
        still have nothing to hand back — somebody saying "change my shop's
        name, you got it wrong" has named an intent and no name. Every one of
        those fell through to the fallback, which is how a system that
        understood you still looks like it did not.
    """
    convo.context = {**convo.context, _ASKING_FOR: what}
    return Outcome([Reply(_MISSING[what])])


def _found(
    db: Session,
    seller: Seller,
    convo: WaConversation,
    products: list[Product],
    *,
    heading: str | None = None,
    ceiling: int | None = None,
) -> list[Reply]:
    """
    Show what a search turned up — one product in full, or a pickable list.

    Shared by the literal search, the budget parser and the model, so all three
    produce the same screen. Three near-identical renderings of a product list
    is how they drift into disagreeing about what a price looks like.
    """
    if len(products) == 1:
        return _show_product(convo, products[0])

    convo.state = ConversationState.LISTING
    convo.context = {"products": [p.id for p in products], "category": None}

    lines = [
        f"{i}. {product.title} — {product.price_display or 'Ask for price'}"
        for i, product in enumerate(products, start=1)
    ]
    if ceiling is not None:
        title = f"Here is what we have under *KES {ceiling:,}*:"
    elif heading:
        title = f'Here is what we have for "{heading}":'
    else:
        title = "Here is what we have:"

    return [
        Reply(
            title + "\n\n" + "\n".join(lines),
            rows=[
                (
                    f"prod:{product.id}",
                    product.title,
                    product.price_display or "Ask for price",
                )
                for product in products
            ],
            list_label="See items",
        )
    ]
