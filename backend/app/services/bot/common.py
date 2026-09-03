"""
Low-level conversation and lookup helpers shared across the bot.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Cart,
    ConversationState,
    Seller,
    WaConversation,
)
from app.services.bot.replies import Reply
from app.services.cart import get_or_create_cart


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


def _find_shop(db: Session, wanted: str) -> Seller | None:
    """
    The shop a buyer named, by slug or by the name on the sign.

    Notes:
        BOTH, BECAUSE LINKS IN CIRCULATION CARRY EITHER. The share link now
        prefills "shop:<slug>" — unambiguous, and it resolves to exactly one
        shop. Older links prefilled the display name ("Shop Book Lounge"), so
        the name is still matched too and none of those break.

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


def _tell_seller(seller: Seller, reply: Reply) -> list[tuple[str, Reply]]:
    """
    Address one message to a seller, if we have a number to send it to.

    Returns:
        A single ``(phone, Reply)``, or nothing at all.

    Notes:
        EMPTY RATHER THAN A GUESS. A seller with no WhatsApp number cannot be
        reached, and the database already refuses to publish such a shop — so in
        practice this is belt and braces. It returns a list so callers can
        splice it in without checking.

        WHAT THIS CANNOT FIX. Meta's 24-hour window means a free-form message
        only reaches somebody who has written to us within the last day. A
        seller who has not opened the thread since yesterday will not get this,
        and the send fails silently at the webhook. The order is still there and
        `orders` still lists it — which is why that screen exists, and why the
        alert is a convenience rather than the system of record. Making it
        reliable needs an approved utility template, which is its own piece of
        work.
    """
    phone = (seller.whatsapp_number or "").strip()
    return [(phone, reply)] if phone else []
