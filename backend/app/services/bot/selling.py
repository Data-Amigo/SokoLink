"""
The seller's side of the thread: forward, price, publish, open, orders.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ConversationState,
    Order,
    OrderStatus,
    PriceSource,
    Product,
    ProductStatus,
    Seller,
    WaConversation,
)
from app.schemas.conversation import Intent
from app.services.accounts import SignupError, create_account_for_phone, reserve_slug
from app.services.bot.common import get_conversation
from app.services.bot.presentation import _ask_for, _share_card, _share_link, _shop_card
from app.services.bot.reading import _understand
from app.services.bot.replies import _INTAKING, PAGE_SIZE, Outcome, Reply, _plural
from app.services.catalogue import PublishError, publish_product
from app.services.notifications import (
    buyer_receipt,
)
from app.services.orders import (
    OrderError,
    confirm_payment,
    get_payment_method,
)
from app.services.payment_methods import PaymentSetupError, save_payment_method
from app.services.questions import answer, get_for_seller, open_questions


def summarise_intake(db: Session, seller: Seller) -> list[Reply]:
    """
    One message for a whole burst of forwarded photos. Sent by the worker.

    The webhook enqueues a parse job per photo and answers instantly; the LAST
    of those jobs calls this. It reads the drafts the burst produced, sets the
    pricing state, and returns the one reply the seller sees — the price
    question for the first unpriced item, carrying "(N left)" so they know the
    whole batch landed. Replacing the per-photo "Added X" trickle with this is
    the point of the change.

    Args:
        db: Session. The worker commits.
        seller: Whose burst just finished parsing.

    Returns:
        The replies to send the seller — never more than a couple.

    Notes:
        A PHOTO THE MODEL COULD NOT READ LEAVES NO DRAFT, and its error is
        already cached against the media id. A burst where every photo failed
        produces no drafts, so the seller is told so rather than met with
        silence.

        THE PRICING QUEUE IS EVERY UNPRICED DRAFT, not only this burst's. A
        seller who left items unpriced last time and forwards more should be
        asked about all of them, not have the earlier ones stranded.
    """
    phone = seller.whatsapp_number
    if not phone:
        # A seller with no number cannot be messaged or keyed to a conversation.
        # In practice unreachable — a seller is recognised BY their number — but
        # the type is nullable and silence beats a crash in the worker.
        return []
    convo = get_conversation(db, phone)
    # Clear the ack flag whatever happens next, so the following burst is
    # acknowledged again rather than met with silence.
    convo.context = {k: v for k, v in convo.context.items() if k != _INTAKING}

    drafts = db.scalars(
        select(Product).where(
            Product.seller_id == seller.id,
            Product.status == ProductStatus.DRAFT.value,
        )
    ).all()
    if not drafts:
        return [
            Reply(
                "I couldn't read those photos. Send clearer ones, or type the item and its price."
            )
        ]

    unpriced = [p for p in drafts if p.price_kes is None]
    if unpriced:
        # The same state and the same prompt the synchronous path used — one
        # item at a time, by name, with the count still to go.
        convo.state = ConversationState.PRICING
        convo.context = {**convo.context, "pricing_queue": [p.id for p in unpriced]}
        return _pricing_prompt(db, seller, convo)

    # Every draft already carries a price the model could see. Nothing to ask;
    # say what is ready and offer the one action that follows.
    convo.state = ConversationState.NEW
    return _priced_summary(db, seller)


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


def _rename_shop(db: Session, seller: Seller, name: str) -> list[Reply]:
    """
    Change what a shop is called, and its link if nobody has that link yet.

    Notes:
        THE SLUG IS PERMANENT ONCE PUBLISHED. It is the address the seller has
        already put in their status and their bio, and re-deriving it from a new
        display name would silently break every link they had shared. Before
        publishing, nobody has it, so it is regenerated to match — which is what
        rescues a shop misnamed during signup.

        THIS EXISTS BECAUSE SOMEBODY NEEDED IT AND COULD NOT HAVE IT. A seller
        answered "what's your shop called?" with "My shop is called Biggie
        Books", got a shop named after the whole sentence, said "Sorry I mean
        Biggie Books is my brand name" — and there was no way to change it.
    """
    clean = " ".join(name.split())
    if len(clean) < 2 or len(clean) > 60:
        return [Reply("That name won't work — between 2 and 60 characters, please.")]

    was = seller.display_name
    seller.display_name = clean

    if not seller.is_published:
        seller.slug = reserve_slug(db, clean)
    db.flush()

    lines = [f"Changed. *{was}* is now *{clean}*."]
    if seller.is_published:
        # Said plainly, because a seller who expects their link to follow the
        # name will hand out a dead one otherwise.
        lines.append(f"_Your shop link still ends in /{seller.slug}, so it keeps working._")

    return [Reply("\n".join(lines), buttons=[("share", "My shop link")])]


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


def _ask_about(seller: Seller, convo: WaConversation) -> list[Reply]:
    """
    Ask the seller to describe their own shop.

    Notes:
        IT SHOWS THEM WHAT IS THERE NOW. A seller who has never set this has
        a description derived from their categories, and one who has set it
        wants to see it before replacing it. Asking somebody to describe
        their shop without showing what they already wrote is how a form
        feels.
    """
    convo.state = ConversationState.ABOUT
    convo.context = {}

    current = seller.bio.strip() if seller.bio and seller.bio.strip() else ""
    lead = (
        f"Buyers currently read this:\n\n_{current}_\n\nSend a new one to replace it."
        if current
        else "Tell buyers what your shop sells, in a sentence."
    )
    return [
        Reply(
            f"{lead}\n\n"
            "_Something like: Vitabu Bora sells school books and revision "
            "materials, delivered around Nairobi._"
        )
    ]


def _save_about(db: Session, seller: Seller, convo: WaConversation, said: str) -> list[Reply]:
    """Store the line, and show it back the way a buyer will see it."""
    text = " ".join(said.split())
    if len(text) < 10 or len(text) > 400:
        return [
            Reply(
                "A bit more than that — one sentence saying what you sell, "
                "between 10 and 400 characters."
            )
        ]

    seller.bio = text
    db.flush()

    convo.state = ConversationState.NEW
    convo.context = {}
    return [
        Reply(
            f"Saved. Every buyer who opens your shop now reads:\n\n_{text}_",
            buttons=[("share", "My shop link")],
        )
    ]


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


def _confirm_order(db: Session, seller: Seller, reference: str) -> Outcome:
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
        return Outcome([Reply("I can't find that order. Send *orders* to see what's waiting.")])

    try:
        confirm_payment(db, order)
    except OrderError as exc:
        return Outcome([Reply(str(exc))])

    # THE BUYER IS TOLD. `_claim` promised them "you'll get a message here
    # when they do" and nothing delivered it — the buyer was left watching a
    # thread that had gone quiet on the one question that mattered to them.
    receipt = Reply(buyer_receipt(order, seller))

    return Outcome(
        [
            Reply(
                f"*{order.reference}* is paid. ✅\n\n"
                f"KES {order.total_kes:,} from {order.buyer_name}.\n\n"
                f"_I've told {order.buyer_name} it's confirmed._",
                buttons=[("orders", "What else is waiting")],
            )
        ],
        notify=[(order.buyer_phone, receipt)],
    )


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

    # THE LINK, IMMEDIATELY. It was held back at first, on the reasoning that
    # an empty shop has nothing worth looking at — which was wrong. The link
    # is the thing that makes the shop feel real, it is what the seller came
    # for, and every shop bot they have used hands it over the moment they
    # give their brand name. It is also copyable text, not a button, because
    # its whole job is to be pasted into a status.
    replies = [
        Reply(
            f"*{seller.display_name}* is yours. 🎉\n\n"
            "It's closed for now — nobody sees it until you say so."
        )
    ]

    link = _share_link(seller)
    if link:
        replies.append(
            Reply(
                f"This is your shop link — it is yours from now on:\n\n{link}\n\n"
                "_It will not show anything until you put something in._"
            )
        )

    replies.append(
        Reply(
            "Now forward me a post from your catalogue. Photo and caption, "
            "exactly as you'd send a customer. I'll read it and set the item "
            "up, and if I can't see a price I'll ask you for one."
        )
    )
    return replies


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


def _seller_questions(db: Session, seller: Seller) -> list[Reply]:
    """Every customer still waiting on this shop for an answer."""
    waiting = open_questions(db, seller)
    if not waiting:
        return [
            Reply(
                "Nobody is waiting on you. 👍\n\n"
                "When a customer asks something I can't answer — delivery, "
                "timing, anything about your terms — it lands here.",
                buttons=[("orders", "My orders")],
            )
        ]

    lines = [f"*{_plural(len(waiting), 'question')} waiting on you*", ""]
    rows: list[tuple[str, str, str]] = []
    for row in waiting:
        lines.append(f"• _{row.question}_")
        rows.append(
            (
                f"answer:{row.id}",
                f"Answer #{row.id}"[:24],
                row.question[:72],
            )
        )

    return [Reply("\n".join(lines), rows=rows, list_label="Answer one")]


def _start_answer(db: Session, seller: Seller, convo: WaConversation, question_id: int) -> Outcome:
    """Put the seller in front of one question, waiting for their words."""
    row = get_for_seller(db, seller, question_id)
    if row is None:
        return Outcome([Reply("I can't find that question. Send *questions* to see what's open.")])
    if not row.is_open:
        return Outcome(
            [
                Reply(
                    f"That one's already answered:\n\n_{row.answer}_",
                    buttons=[("questions", "What else is waiting")],
                )
            ]
        )

    convo.state = ConversationState.ANSWERING
    convo.context = {"question_id": row.id}
    return Outcome(
        [
            Reply(
                f"A customer asked:\n\n_{row.question}_\n\n"
                f"Type your answer and I'll send it to them as it is."
            )
        ]
    )


def _relay_answer(db: Session, seller: Seller, convo: WaConversation, said: str) -> Outcome:
    """
    Send the seller's words to the customer who has been waiting.

    Notes:
        VERBATIM, AND ATTRIBUTED. The buyer reads "Vitabu Bora says: …" so they
        know a person answered and which one. We do not tidy it, shorten it or
        rephrase it — the moment we edit an answer about delivery, the promise
        stops being entirely the seller's.
    """
    question_id = convo.context.get("question_id")
    row = get_for_seller(db, seller, int(question_id)) if isinstance(question_id, int) else None
    if row is None:
        convo.state = ConversationState.NEW
        convo.context = {}
        return Outcome([Reply("I've lost track of that question. Send *questions* to see them.")])

    try:
        answer(db, row, said)
    except ValueError as exc:
        return Outcome([Reply(str(exc))])

    convo.state = ConversationState.NEW
    convo.context = {}

    to_buyer = Reply(f"*{seller.display_name}* says:\n\n{row.answer}")

    return Outcome(
        [
            Reply(
                "Sent. ✅",
                buttons=[("questions", "Anything else waiting")],
            )
        ],
        notify=[(row.buyer_phone, to_buyer)],
    )


def _seller_said_something(db: Session, convo: WaConversation, said: str, owner: Seller) -> Outcome:
    """
    A seller wrote a sentence rather than tapping a button. Work out what it was.

    Notes:
        THE STATUS CARD USED TO BE THE ANSWER TO EVERYTHING. A seller who typed
        "Sorry I mean Biggie Books is my brand name" got their shop status. So
        did "A way to put my catalogue and my shop link". Twice each. Nothing
        was wrong with the card — it was simply not an answer to either
        question, and a system that replies to every sentence with the same
        screen has stopped listening.

        THE CARD IS STILL THE FALLBACK, because a seller with nothing
        outstanding and an unreadable message is best served by seeing where
        they are up to. It is the last resort now rather than the only one.
    """
    reading = _understand(db, convo, said, owner=owner, shopping_at=None)
    if reading is None:
        return Outcome(_seller_home(db, owner))

    if reading.intent is Intent.SHOP_NAME:
        # ASKED FOR, NOT SHRUGGED AT. "Change my shop's name, you got it wrong"
        # is a clear request carrying no new name — the answer is a question.
        if reading.name:
            return Outcome(_rename_shop(db, owner, reading.name))
        return _ask_for(convo, "shop_name")

    if reading.intent is Intent.SET_ABOUT:
        if reading.about:
            return Outcome(_save_about(db, owner, convo, reading.about))
        return _ask_for(convo, "about")

    if reading.intent is Intent.SELLER_ORDERS:
        return Outcome(_seller_orders(db, owner))

    if reading.intent is Intent.FOR_THE_SELLER:
        # A SELLER asking us something. There is nobody to hand this to, so it
        # is answered with what they can actually do — the honest version of
        # "I don't know" is a way forward, not a shrug.
        return Outcome(
            [
                Reply(
                    "I can't answer that one, but here's what I can do for you.",
                    buttons=[("orders", "My orders"), ("questions", "Customer questions")],
                )
            ]
        )

    if reading.intent is Intent.SELLER_PAYMENTS:
        return Outcome(_ask_payment_kind(convo))

    if reading.intent is Intent.SELLER_SHARE_LINK:
        return Outcome([_share_card(owner)])

    if reading.intent is Intent.SELLER_OPEN:
        return Outcome(_open_shop(db, owner))

    if reading.intent is Intent.SELLER_ADD_STOCK:
        return Outcome(
            [
                Reply(
                    "Forward me a post from your catalogue — the photo and the "
                    "caption, exactly as you'd send a customer. I'll read it and "
                    "set the item up, and ask you for a price if I can't see one.",
                    buttons=[("share", "My shop link"), ("orders", "My orders")],
                )
            ]
        )

    # A greeting or a question with no action behind it. The model's own words,
    # then their shop underneath — because "hello" deserves an answer AND a
    # seller opening the thread still wants to know where things stand.
    if reading.may_speak and reading.reply:
        return Outcome([Reply(reading.reply), *_seller_home(db, owner)])

    return Outcome(_seller_home(db, owner))
