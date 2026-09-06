"""
Low-level conversation and lookup helpers shared across the bot.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
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


def find_account_by_phone(db: Session, phone: str) -> Account | None:
    """
    The login this number belongs to, if any — the owner of one or more shops.

    Multi-shop (2026-09) made the account, not the shop, the thing a number maps
    to: one number can own several shops, so the resolver returns the ACCOUNT
    and the caller picks the active shop with :func:`active_shop`.

    Matched on the number as stored and without a country code, the same way
    :func:`find_seller_by_phone` is, because a seller may have signed up with it
    written either way.
    """
    local = "0" + phone[3:] if phone.startswith("254") and len(phone) == 12 else phone
    return db.scalar(select(Account).where(Account.phone.in_({phone, local, f"+{phone}"})))


def owner_context(
    db: Session, convo: WaConversation, phone: str
) -> tuple[Account | None, Seller | None]:
    """
    Resolve who this number is as a SELLER: their account and their active shop.

    Resolution starts from the shop matched by number (:func:`find_seller_by_phone`)
    rather than the account, so it works for every seller — including ones with
    no login account (older rows, and test fixtures). When that shop belongs to
    an account, multi-shop applies and the ACTIVE shop is chosen from the
    account's shops; when it does not, the single shop is the owner and there is
    no account to run multi-shop against.

    Returns:
        ``(account, active_shop)``. Both None for a number that owns no shop;
        ``(None, shop)`` for a legacy shop with no account; ``(account, shop)``
        for the normal multi-shop case.
    """
    base = find_seller_by_phone(db, phone)
    if base is None:
        return None, None
    if base.account is not None:
        return base.account, active_shop(db, convo, base.account)
    return None, base


def active_shop(db: Session, convo: WaConversation, account: Account) -> Seller | None:
    """
    The shop this owner is currently managing on this thread.

    Which of an account's shops a message acts on is the conversation's
    ``managing_shop_id``. When it is unset or points at a shop that is gone
    (deleted, or reassigned), it falls back to the account's primary live shop
    and records that choice so the rest of the turn is consistent.

    Args:
        db: Session.
        convo: The conversation, whose ``managing_shop_id`` is read and, when it
            needs defaulting, written.
        account: The owner.

    Returns:
        The active :class:`Seller`, or None when the account has no live shop
        (every shop archived) — in which case the caller treats them as someone
        with no shop yet.

    Notes:
        ARCHIVED SHOPS ARE NEVER ACTIVE. A deleted shop is soft-deleted
        (``archived_at`` set), so it must never be handed back as the thing an
        owner is managing — it would let them keep adding stock to a shop they
        told us to remove.
    """
    live = [s for s in account.sellers if s.archived_at is None]
    if not live:
        return None

    chosen = next((s for s in live if s.id == convo.managing_shop_id), None)
    if chosen is None:
        # Default to the primary (earliest) shop — the same one Account.seller
        # returns for the web — and remember it for the rest of the thread.
        chosen = live[0]
        convo.managing_shop_id = chosen.id
    return chosen


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
