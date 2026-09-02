"""
The shop, as a conversation. No browser anywhere in it.

    inbound text ──▶ handle() ──▶ [Reply, ...] ──▶ Cloud API sends each reply

WHY THE CHAT IS THE SHOP. A web link is the one thing we cannot control: tapping
a URL hands an intent to the operating system, and some Android skins route it
to the default browser however the message was built. Tested on a real handset —
plain link and native CTA button both left WhatsApp. So the buyer surface stops
being a page we link to and becomes the thread itself, which no OS can redirect.

NATIVE COMPONENTS, WITH THE TEXT STILL UNDERNEATH. Replies carry buttons and
list rows, which Meta's Cloud API draws as real tap targets — free-form, inside
the 24-hour window a person opens by messaging first, with no template
approval. That approval requirement is why an earlier version of this file
shipped numbered text menus.

The body ALWAYS names every option anyway. A client that cannot render a
component gets the text; some clients draw neither. A reply whose body reads
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

from sqlalchemy.orm import Session

from app.models import (
    ConversationState,
    Product,
    Seller,
    WaConversation,
)
from app.schemas.conversation import Intent
from app.services.bot.buying import _buyer_said_something, _claim, _hand_off, _place
from app.services.bot.common import _basket, _find_shop, find_seller_by_phone, get_conversation
from app.services.bot.presentation import (
    _add_to_basket,
    _ask_address,
    _ask_delivery,
    _ask_variant,
    _found,
    _list_products,
    _menu,
    _share_card,
    _shop_card,
    _show_cart,
    _show_product,
    _start_checkout,
    _still_reading,
)
from app.services.bot.reading import _extracted, _understand
from app.services.bot.replies import (
    _ASKING_FOR,
    _INTAKING,
    _PAY_ALIASES,
    PAGE_SIZE,
    Outcome,
    Reply,
    _digits,
    _max_price,
    _price,
)
from app.services.bot.selling import (
    _ask_about,
    _ask_payment_kind,
    _ask_payment_number,
    _ask_shop_name,
    _confirm_order,
    _create_shop,
    _open_shop,
    _priced_summary,
    _pricing_prompt,
    _publish_ready,
    _relay_answer,
    _rename_shop,
    _resume_pricing,
    _save_about,
    _save_payment,
    _seller_home,
    _seller_orders,
    _seller_questions,
    _seller_said_something,
    _set_price,
    _start_answer,
    _welcome,
    summarise_intake,
)
from app.services.cart import clear
from app.services.intake import PARSE_FORWARD, MediaFetch
from app.services.jobs import enqueue
from app.services.storefront import get_public_products

__all__ = [
    "handle",
    "summarise_intake",
    "Reply",
    "Outcome",
    "get_conversation",
    "find_seller_by_phone",
]


def _answer_to_what_we_asked(
    db: Session, convo: WaConversation, said: str, *, owner: Seller | None, seller: Seller | None
) -> Outcome | None:
    """
    Take the answer to the question :func:`_ask_for` put, if one is pending.

    Returns:
        What to do, or None when nothing was outstanding.
    """
    what = convo.context.get(_ASKING_FOR)
    if not isinstance(what, str) or not said.strip():
        return None

    convo.context = {k: v for k, v in convo.context.items() if k != _ASKING_FOR}

    if what == "shop_name" and owner is not None:
        # READ, NOT TAKEN WHOLE. Knowing which question is outstanding is not
        # the same as the reply being only the answer — "My shop name should be
        # Biggie Books" is a sentence with a name in it, and taking it verbatim
        # is the very bug the extraction exists to fix. This path bypassed it
        # once already, and named a shop "My shop name should be Biggie Books".
        return Outcome(_rename_shop(db, owner, _extracted(db, convo, said, owner, "name")))

    if what == "about" and owner is not None:
        # A description IS the sentence, so the raw text is the right answer
        # here — but the model still gets a look, because "tell them we sell
        # school books" should store the description, not the instruction.
        return Outcome(_save_about(db, owner, convo, _extracted(db, convo, said, owner, "about")))
    if what in {"query", "budget"} and seller is not None:
        # Handed back to the ordinary buyer path, which already knows how to
        # search a catalogue and read a budget out of a sentence.
        return None
    return None


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
            rather than a URL because Meta serves media as an id resolved
            through two authenticated Graph calls, not a public URL — and doing
            that is the webhook's business, not the conversation's.

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
            # THE PARSE DOES NOT RUN HERE. It is a paid vision call, and Meta
            # redelivers any webhook that is slow — so parsing eight photos in
            # the request path was eight serial calls that timed out and came
            # back out of order, the exact trickle this change removes. Enqueue
            # one job per photo instead; the worker downloads, reads and drafts,
            # and the LAST job of the burst sends one summary. dedupe_key means
            # a redelivered photo is neither parsed nor billed twice.
            for media_id, _fetch in media:
                enqueue(
                    db,
                    PARSE_FORWARD,
                    payload={"seller_id": owner.id, "media_id": media_id, "caption": said},
                    seller_id=owner.id,
                    dedupe_key=f"parse:{media_id}",
                )

            # ONE acknowledgement per burst, not one per photo. Meta delivers a
            # forwarded album as several separate messages, so acking each would
            # rebuild the wall of noise. The flag is cleared when the summary
            # goes out, so the next burst is acknowledged again.
            if convo.context.get(_INTAKING):
                return Outcome([])
            convo.context = {**convo.context, _INTAKING: True}
            return Outcome(
                [Reply("📸 Got it — reading your items now. I'll list them here in a moment.")]
            )

    # ── Routing: which shop is this? ────────────────────────────────────────
    # The shareable link is wa.me/<bot>?text=shop%20<slug>, so the buyer's very
    # first message names the shop. That link opens WhatsApp itself — it is a
    # deep link, not a URL, so nothing can redirect it to a browser.
    if lowered.startswith("shop ") or lowered.startswith("shop:"):
        # BOTH SEPARATORS. Links in circulation use a space; the colon form is
        # the convention several Kenyan shop bots use, and a seller who copies
        # that style must not hand out a link that lands in the fallback.
        wanted = said[5:].strip().lstrip(":").strip()
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
        return Outcome([*switched, *_menu(db, seller, convo, greet=True), _shop_card(seller)])

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

        # WHAT WE JUST ASKED FOR COMES FIRST. A seller answering "Biggie
        # Books" to "what should it be called instead?" must not have that
        # word matched against commands or handed to the model again.
        pending = _answer_to_what_we_asked(db, convo, said, owner=owner, seller=None)
        if pending is not None:
            return pending

        if convo.state == ConversationState.ANSWERING:
            if lowered in {"cancel", "stop", "later"}:
                convo.state = ConversationState.NEW
                convo.context = {}
                return Outcome(_seller_home(db, owner))
            return _relay_answer(db, owner, convo, said)

        if lowered.startswith("answer:"):
            tail = said.split(":", 1)[1].strip()
            if tail.isdigit():
                return _start_answer(db, owner, convo, int(tail))

        if lowered in {"questions", "my questions", "customer questions"}:
            return Outcome(_seller_questions(db, owner))

        if convo.state == ConversationState.ABOUT:
            if lowered in {"cancel", "stop", "later"}:
                convo.state = ConversationState.NEW
                convo.context = {}
                return Outcome(_seller_home(db, owner))
            return Outcome(_save_about(db, owner, convo, said))

        if lowered in {"about", "about my shop", "describe my shop", "my description"}:
            return Outcome(_ask_about(owner, convo))
        if lowered in {"orders", "my orders", "sales"}:
            return Outcome(_seller_orders(db, owner))
        if lowered.startswith("confirm:"):
            return _confirm_order(db, owner, said.split(":", 1)[1].strip())
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
            # MID-FORWARD CHATTER. While a burst of photos is still being read,
            # a sentence like "are you adding one by one?" is a question ABOUT
            # that batch, and handing it to the model produced the shrug in the
            # screenshots. Reassure them; the summary is on its way. Commands
            # (orders, payments, share) matched above still work while reading.
            if convo.context.get(_INTAKING):
                return Outcome(_still_reading())
            return _seller_said_something(db, convo, said, owner)

    # ── Somebody with no shop, who has not opened one either ────────────────
    if convo.state == ConversationState.NAMING:
        # THE BIGGIE BOOKS FIX. "My shop is called Biggie Books" used to become
        # a shop named after the whole sentence, permanently, with no way back.
        # The model pulls the name out; the raw text is the fallback when there
        # is no model, which is what we had.
        reading = _understand(db, convo, said, owner=None, shopping_at=None)
        # Named apart from the numeric `chosen` further down this function —
        # that one is a menu position and this one is a shop's name.
        wanted_name = (
            reading.name
            if reading is not None and reading.intent is Intent.SHOP_NAME and reading.name
            else said
        )
        return Outcome(_create_shop(db, convo, phone, wanted_name))

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
    # SAYING HELLO IS SAYING HELLO. Answered with the shop's name and what it
    # has in — not with a bare question, which is what a form does.
    if lowered in {"hi", "hello", "start", "habari", "niaje", "mambo", "sasa"}:
        return Outcome(_menu(db, seller, convo, greet=True))

    # Tapping "Keep shopping" is not a greeting. Introducing the shop again
    # here is the repetition that made the thread feel like a machine.
    if lowered in {"menu", "keep shopping", "see what's in stock", "start again"}:
        return Outcome(_menu(db, seller, convo))

    if lowered in {"store", "shop", "website", "open store", "see everything"}:
        # The only reply that can open a page inside WhatsApp — see _shop_card.
        return Outcome([_shop_card(seller)])

    # A buyer answering "what are you looking for?" is answering, not issuing
    # a command. Cleared here so the search below sees their words plainly.
    if convo.context.get(_ASKING_FOR) in {"query", "budget"}:
        convo.context = {k: v for k, v in convo.context.items() if k != _ASKING_FOR}

    if lowered in {"ask", "ask the shop", "ask the seller"}:
        convo.context = {**convo.context, "awaiting_question": True}
        return Outcome(
            [
                Reply(
                    f"What would you like to ask *{seller.display_name}*?\n\n"
                    "_Delivery, timing, anything about the item — I'll pass it "
                    "straight on._"
                )
            ]
        )

    if convo.context.get("awaiting_question") and len(said) >= 3:
        convo.context = {k: v for k, v in convo.context.items() if k != "awaiting_question"}
        return _hand_off(db, seller, phone, said)

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
            return _place(db, seller, convo, phone, name=name, address=None)
        if lowered in {"deliver", "deliver to me", "delivery"}:
            return Outcome(_ask_address(convo))
        return Outcome(_ask_delivery(convo, name))

    elif convo.state == ConversationState.ADDRESS:
        address = said.strip()
        if len(address) < 3:
            return Outcome(_ask_address(convo))
        return _place(
            db,
            seller,
            convo,
            phone,
            name=str(convo.context.get("buyer_name", "")),
            address=address,
        )

    elif convo.state == ConversationState.PAYING:
        return _claim(db, convo, said)

    # ── Taking them at their word ───────────────────────────────────────────
    # A buyer who types "a revision book" or "anything under 1000" has said
    # exactly what they want. These two paths are FREE — a literal title match
    # and a regex — so they run before anything is spent on a model.
    ceiling = _max_price(said)
    if ceiling is not None:
        found = get_public_products(db, seller, max_price_kes=ceiling)[:PAGE_SIZE]
        if found:
            return Outcome(_found(db, seller, convo, found, ceiling=ceiling))
        return Outcome(
            [
                Reply(
                    f"I don't have anything under *KES {ceiling:,}* right now.",
                    buttons=[("menu", "See everything")],
                )
            ]
        )

    if len(said) >= 3 and _digits(said) is None:
        found = get_public_products(db, seller, search=said)[:PAGE_SIZE]
        if found:
            return Outcome(_found(db, seller, convo, found, heading=said))

    # ── A sentence the free paths could not read ────────────────────────────
    # Everything above is exact and costs nothing: ids, commands, numbers, a
    # literal title search, a budget. What reaches here is a person talking —
    # "do you have anything for a nine year old", "is there something cheaper",
    # "asante sana" — and answering that with a menu is what made the thread
    # feel like a machine.
    spoken = _buyer_said_something(db, seller, convo, said)
    if spoken is not None:
        return spoken

    # ── Anything we still could not read ────────────────────────────────────
    # Re-offering the menu beats "I didn't understand": the buyer's problem is
    # not that we failed to parse, it is that they cannot see their options.
    #
    # NOT GREETED AGAIN, THOUGH. Landing here mid-purchase must not read as
    # being met at the door by somebody who has forgotten the last ten minutes.
    return Outcome(_menu(db, seller, convo))
