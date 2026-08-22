"""
Setting up where a seller's money goes.

    seller + kind + number ──▶ PaymentMethod ──▶ checkout can happen
                    │
                    └── optional Daraja credentials ──▶ STK prompts too

WHY THIS IS THE MOST IMPORTANT SCREEN A SELLER NEVER THINKS ABOUT. Until a
payment method exists, ``place_order`` refuses and the storefront cannot take a
single order. A shop that looks finished and cannot be bought from is worse than
one that says it is not ready.

THE DEFAULT IS POCHI, AND THAT IS DELIBERATE. It is what a seller with no
business registration already has, it needs nothing from Safaricom, and it works
the moment they type their number. Till and Paybill are the upgrade, not the
baseline — a setup flow that opened by asking for a shortcode would lose most of
our sellers at the first field.

CREDENTIALS ARE OPTIONAL, ALWAYS. A Till seller who does not want to hand over
their consumer secret still sells; they simply confirm payments by hand like a
Pochi seller. Nothing here may make automatic confirmation a prerequisite for
taking money.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentMethod, PaymentMethodKind, Seller
from app.secrets_vault import encrypt

#: Kenyan till and paybill numbers are short numeric codes. Loose on purpose —
#: Safaricom has issued 5, 6 and 7 digit codes, and rejecting a valid one
#: because our rule was tighter than reality is worse than accepting a typo the
#: seller will spot on their first order.
MIN_SHORTCODE_DIGITS = 5
MAX_SHORTCODE_DIGITS = 9


class PaymentSetupError(Exception):
    """Setup was refused, with a message safe to show the seller."""


def _clean_number(kind: PaymentMethodKind, raw: str) -> str:
    """
    Validate the destination for this kind of method.

    Pochi is a phone line, so it is normalised to the 254… form M-Pesa and
    ``wa.me`` both use. A till or paybill is a short code and is kept exactly as
    issued — reformatting it would show the seller a number they do not
    recognise from their own statement.

    Raises:
        PaymentSetupError: If it cannot be read as the right kind of number.
    """
    digits = "".join(c for c in raw if c.isdigit())

    if kind is PaymentMethodKind.POCHI:
        # Imported here rather than at module scope: services/daraja.py pulls in
        # httpx and the vault, and setup should not drag the payment stack in.
        from app.services.daraja import DarajaError, normalise_phone

        try:
            return normalise_phone(raw)
        except DarajaError as exc:
            raise PaymentSetupError(
                "Enter the Safaricom number your Pochi la Biashara is on."
            ) from exc

    if not (MIN_SHORTCODE_DIGITS <= len(digits) <= MAX_SHORTCODE_DIGITS):
        raise PaymentSetupError(
            f"A {kind.value} number is {MIN_SHORTCODE_DIGITS}–{MAX_SHORTCODE_DIGITS} digits."
        )
    return digits


def save_payment_method(
    db: Session,
    seller: Seller,
    *,
    kind: str,
    number: str,
    account_name: str | None = None,
    account_reference: str | None = None,
    stk_shortcode: str | None = None,
    consumer_key: str | None = None,
    consumer_secret: str | None = None,
    passkey: str | None = None,
) -> PaymentMethod:
    """
    Create or replace this seller's payment destination.

    Args:
        db: Session.
        seller: Whose shop.
        kind: pochi | till | paybill.
        number: What the buyer pays to.
        account_name: The name M-Pesa shows the buyer on the prompt.
        account_reference: For a paybill that needs one.
        stk_shortcode: The API shortcode, if enabling automatic confirmation.
        consumer_key: Daraja consumer key, plaintext. Encrypted before storage.
        consumer_secret: Daraja consumer secret, plaintext.
        passkey: Daraja passkey, plaintext.

    Returns:
        The saved method.

    Raises:
        PaymentSetupError: On an unknown kind, a malformed number, credentials
            offered for Pochi (Daraja cannot push to it), or a partial set of
            credentials.

    Notes:
        UPDATES IN PLACE when a method already exists, rather than inserting.
        The unique constraint on ``seller_id`` would refuse a second row anyway,
        and a seller changing their till expects to change it, not to be told
        they already have one.

        BLANK CREDENTIAL FIELDS LEAVE THE STORED ONES ALONE. A seller editing
        their till number should not have to re-enter a passkey they cannot see
        — the form never renders secrets back, so an empty box means "unchanged",
        never "delete".
    """
    try:
        kind_enum = PaymentMethodKind(kind)
    except ValueError as exc:
        raise PaymentSetupError("Choose how you want to be paid.") from exc

    cleaned_number = _clean_number(kind_enum, number)

    supplied = {
        "stk_shortcode": (stk_shortcode or "").strip() or None,
        "consumer_key": (consumer_key or "").strip() or None,
        "consumer_secret": (consumer_secret or "").strip() or None,
        "passkey": (passkey or "").strip() or None,
    }
    any_supplied = any(supplied.values())

    if any_supplied and not kind_enum.supports_stk:
        raise PaymentSetupError(
            "Pochi la Biashara cannot receive M-Pesa prompts, so it needs no API "
            "details. You will confirm payments yourself."
        )

    # Queried rather than read off ``seller.payment_method``: a method created
    # elsewhere in this session (a factory, another service) leaves the
    # relationship stale at None, and trusting it would attempt a second INSERT
    # against a UNIQUE seller_id. Assigning through the relationship afterwards
    # is what keeps the in-memory object correct for the rest of the request.
    method = db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id))

    if method is None:
        method = PaymentMethod(kind=kind_enum.value, number=cleaned_number)
        seller.payment_method = method
        db.add(method)
    else:
        method.kind = kind_enum.value
        method.number = cleaned_number

    method.account_name = (account_name or "").strip() or None
    method.account_reference = (account_reference or "").strip() or None

    if not kind_enum.supports_stk:
        # Switching to Pochi clears any credentials, because the database
        # refuses to hold them and, more importantly, they are no longer usable.
        method.stk_shortcode = None
        method.consumer_key_enc = None
        method.consumer_secret_enc = None
        method.passkey_enc = None
    elif any_supplied:
        # All four or nothing. Three of four fails at the STK call, with a buyer
        # waiting — the worst possible place to discover a config mistake.
        already = method.passkey_enc is not None
        if not already and not all(supplied.values()):
            raise PaymentSetupError(
                "To take M-Pesa prompts automatically, enter all four: shortcode, "
                "consumer key, consumer secret and passkey. Leave them all blank to "
                "confirm payments yourself."
            )
        if supplied["stk_shortcode"]:
            method.stk_shortcode = supplied["stk_shortcode"]
        if supplied["consumer_key"]:
            method.consumer_key_enc = encrypt(supplied["consumer_key"])
        if supplied["consumer_secret"]:
            method.consumer_secret_enc = encrypt(supplied["consumer_secret"])
        if supplied["passkey"]:
            method.passkey_enc = encrypt(supplied["passkey"])

    db.flush()
    return method


def disable_stk(db: Session, seller: Seller) -> PaymentMethod | None:
    """
    Forget a seller's Daraja credentials, keeping their payment destination.

    Offered because custody should be reversible: a seller who changes their
    mind about handing us secrets can withdraw them without losing their shop
    or their orders. They simply go back to confirming payments by hand.
    """
    method = db.scalar(select(PaymentMethod).where(PaymentMethod.seller_id == seller.id))
    if method is None:
        return None

    method.stk_shortcode = None
    method.consumer_key_enc = None
    method.consumer_secret_enc = None
    method.passkey_enc = None
    db.flush()
    return method
