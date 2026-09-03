"""
Outbound message shapes, the pure parsers, and the shared constants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VARIANT_LIMIT = 10


PAGE_SIZE = 8


_INTAKING = "intaking"


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
        THE BODY IS NEVER JUST A HEADER FOR THE COMPONENT. Clients render
        these differently — Meta draws real buttons; a client that cannot gets
        the options flattened into numbered text — and one that shows neither
        must still be able to act. A reply whose body is "Choose:" is a dead end
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

    #: A Multi-Product Message, as ``(header, [(section_title, [retailer_id, …])])``.
    #: The buyer taps product cards into a native WhatsApp cart and sends it back
    #: as an ``order`` message. The catalogue id is the deployment's, added at
    #: send time — the conversation names the products, not the catalogue.
    product_list: tuple[str, list[tuple[str, list[str]]]] | None = None


@dataclass
class Outcome:
    """
    What one inbound message produced.

    Args:
        replies: Messages back to whoever sent the inbound one.
        notify: ``(phone, Reply)`` for OTHER people the message concerns.

    Notes:
        WHY THE SECOND LIST EXISTS. A conversation is not only between us and
        the person typing. A buyer placing an order is news the seller needs
        without having to think to ask for it, and a seller confirming a payment
        is news the buyer was explicitly promised — "you'll get a message here
        when they do" was already in the copy, unbacked by anything.

        Without this the loop only closed for shops on STK, because the Daraja
        callback was the single place anything notified anybody. Every Pochi
        seller — most of them — had a silent one.

        SENT AFTER THE COMMIT, like replies, and failures are swallowed the same
        way. An order that exists but whose alert did not send is recoverable:
        the seller sends `orders` and sees it. An alert that sent for an order
        that was rolled back is not.
    """

    replies: list[Reply] = field(default_factory=list)
    notify: list[tuple[str, Reply]] = field(default_factory=list)


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


_PAY_ALIASES: dict[str, str] = {
    "pochi": "pochi",
    "pochi la biashara": "pochi",
    "till": "till",
    "till number": "till",
    "buy goods": "till",
    "paybill": "paybill",
    "pay bill": "paybill",
}


_BUDGET = re.compile(
    r"(?i)\b(?:under|below|less than|cheaper than|upto|up to|max|maximum|si zaidi ya)\b"
    r"[^0-9]{0,10}(?:ksh|kes)?\s*([0-9][0-9,]{1,6})"
)


def _max_price(text: str) -> int | None:
    """
    The ceiling a buyer named, or None.

    Notes:
        THE GREETING PROMISES THIS. It offers "What do you have under 1000?" as
        an example, and an example that drops the buyer into a fallback teaches
        them the shop does not listen. The suggestion was written first and this
        was built to make it true.
    """
    match = _BUDGET.search(text)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:  # pragma: no cover - the pattern only matches digits
        return None


def _plural(count: int, word: str) -> str:
    """``1 item`` / ``3 items``. Copy that says "1 items" reads as a bug."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


_ASKING_FOR = "awaiting"
