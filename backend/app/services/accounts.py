"""
Signup, login, and the shop that comes with an account.

    signup ──> Account (hashed) ──> Seller (slug reserved) ──> session token
    login  ──> verify ──> session token

WHY signup creates a Seller too: a login with no shop is a dead end. Every
account exists to run a storefront, so the shop is created in the same
transaction — either both exist or neither does.

ANTI-ENUMERATION IS THE POINT OF THIS FILE. A stranger must not be able to
learn which email addresses are registered, by response text or by timing. See
:func:`authenticate`.
"""

from __future__ import annotations

import re
import secrets
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Account, Seller
from app.security import (
    DUMMY_HASH,
    AuthError,
    hash_password,
    needs_rehash,
    validate_password_strength,
    verify_password,
)

#: Slugs shorter than this are hard to make unique and read badly in a URL.
MIN_SLUG_LENGTH = 3
MAX_SLUG_LENGTH = 40

#: Words that must never become a storefront slug, because a seller using one
#: could impersonate the platform: a buyer who lands on a shop called "support"
#: or "billing" has no way to tell it is not us asking.
#:
#: THIS LIST USED TO BE TWICE AS LONG. Storefronts once lived at `/{slug}`, so
#: it also had to name every route we had or might ever add — "health", "docs",
#: "login" — and adding a route without adding a word here would silently make
#: some seller's shop unreachable. Moving the storefront to `/shop/{slug}` made
#: collisions impossible, so the list now does one job instead of two.
RESERVED_SLUGS = frozenset(
    {
        "admin",
        "administrator",
        "biashara",
        "biasharamall",
        "billing",
        "help",
        "moderator",
        "official",
        "security",
        "staff",
        "support",
        "system",
        "www",
    }
)


class SignupError(Exception):
    """Signup could not proceed, with a message safe to show the user."""


def slugify(value: str) -> str:
    """
    Turn a shop name into a URL-safe slug.

    Args:
        value: Whatever the seller typed as their shop name.

    Returns:
        A lowercase, dash-separated slug. May be empty if the input had no
        usable characters — callers must handle that.
    """
    # Strip accents rather than dropping the letters entirely, so "Café" becomes
    # "cafe" and not "caf".
    normalised = unicodedata.normalize("NFKD", value)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug[:MAX_SLUG_LENGTH].strip("-")


def reserve_slug(db: Session, desired: str) -> str:
    """
    Find a free slug close to what the seller wanted.

    Appends -2, -3, … on collision rather than failing, because a seller whose
    shop name is already taken should not be stopped at signup — they can
    change it before publishing, and the slug only becomes permanent then.

    Args:
        db: Session to check existing slugs against.
        desired: The seller's preferred slug or shop name.

    Returns:
        A slug not currently in use.

    Raises:
        SignupError: If nothing usable can be derived from the input.
    """
    base = slugify(desired)

    if len(base) < MIN_SLUG_LENGTH or base in RESERVED_SLUGS:
        raise SignupError("Please choose a shop name with at least three letters or numbers.")

    candidate = base
    suffix = 2
    while db.scalar(select(Seller.id).where(Seller.slug == candidate)) is not None:
        candidate = f"{base[: MAX_SLUG_LENGTH - 4]}-{suffix}"
        suffix += 1

    return candidate


def normalise_whatsapp(raw: str | None) -> str | None:
    """
    Normalise a Kenyan number to the 2547XXXXXXXX form wa.me and M-Pesa share.

    Sellers type what they know — ``0712 345 678``, ``+254712345678``. Storing
    it verbatim would mean every ``wa.me`` link built from it is a coin toss, so
    it is normalised once, here, on the way in.

    Args:
        raw: Whatever was typed, or None.

    Returns:
        The normalised number, or None when nothing was given.

    Raises:
        SignupError: If it cannot be read as a Kenyan mobile number.
    """
    if raw is None or not raw.strip():
        return None

    # Imported inside the function: services/daraja.py pulls in httpx and the
    # vault, and signup should not drag the payment stack in.
    from app.services.daraja import DarajaError, normalise_phone

    try:
        return normalise_phone(raw)
    except DarajaError as exc:
        raise SignupError(
            "Enter the WhatsApp number buyers should use, e.g. 0712 345 678."
        ) from exc


def create_account(
    db: Session,
    *,
    email: str,
    password: str,
    shop_name: str,
    whatsapp_number: str | None = None,
    full_name: str | None = None,
) -> Account:
    """
    Create a login and its shop, in one transaction.

    Args:
        db: Session. The caller commits.
        email: Lowercased before storage.
        password: Plaintext; hashed here and never stored otherwise.
        shop_name: Becomes the display name and the basis for the slug.
        whatsapp_number: The number buyers reach the seller on, normalised to
            2547XXXXXXXX. Optional here so an account can exist without one —
            but a shop CANNOT OPEN without it, because ``published_needs_whatsapp``
            refuses a live storefront nobody can contact. Collected at signup so
            no seller reaches the publish step and finds themselves stuck.
        full_name: Optional.

    Returns:
        The new account, with ``seller`` populated.

    Raises:
        SignupError: On a weak password, an unusable shop name, or an email
            already registered.
    """
    clean_email = email.strip().lower()
    if "@" not in clean_email[1:]:
        raise SignupError("Please enter a valid email address.")

    try:
        validate_password_strength(password)
    except ValueError as exc:
        raise SignupError(str(exc)) from exc

    clean_shop = shop_name.strip()
    if not clean_shop:
        raise SignupError("Please enter a shop name.")

    existing = db.scalar(select(Account.id).where(func.lower(Account.email) == clean_email))
    if existing is not None:
        # Signup can say this; LOGIN never can. A stranger reaching this
        # message already claimed the address, and hiding it here would leave
        # them stuck with no way to understand why signup failed.
        raise SignupError("An account with that email already exists. Try signing in.")

    slug = reserve_slug(db, clean_shop)

    clean_phone = normalise_whatsapp(whatsapp_number)

    if clean_phone is not None:
        taken = db.scalar(select(Account.id).where(Account.phone == clean_phone))
        if taken is not None:
            raise SignupError("That WhatsApp number is already registered. Try signing in.")

    account = Account(
        email=clean_email,
        password_hash=hash_password(password),
        # The phone is the identity a seller actually remembers, and the one
        # WhatsApp can verify. Stored on the account so it can be signed in
        # with; mirrored onto the shop because that is what buyers contact.
        phone=clean_phone,
        full_name=full_name.strip() if full_name else None,
    )
    account.seller = Seller(
        slug=slug,
        display_name=clean_shop,
        whatsapp_number=clean_phone,
    )

    db.add(account)
    db.flush()
    return account


def authenticate(db: Session, *, identifier: str, password: str) -> Account:
    """
    Verify credentials and return the account.

    Args:
        db: Session.
        identifier: A WhatsApp number OR an email, as typed. The number is the
            one a Kenyan seller actually remembers and the one WhatsApp can
            verify, so it is tried first; email still works for anyone who
            signed up with one.
        password: Plaintext, compared against the stored hash.

    Returns:
        The authenticated account.

    Raises:
        AuthError: On any failure. Deliberately one error for every cause.

    Notes:
        Three anti-enumeration measures, all load-bearing:

        1. **One message for every failure** — unknown email, wrong password,
           and deactivated account are indistinguishable.
        2. **Constant-ish timing** — when no account is found we still verify
           against ``DUMMY_HASH``, so a missing email costs the same CPU as a
           wrong password. Without it, response time is an oracle.
        3. **No early return** before that hash comparison.
    """
    typed = identifier.strip()

    # A phone lookup first, and only if the input can BE a phone. Trying both
    # unconditionally would mean two queries on every login, and the timing
    # difference between one and two is exactly the oracle note 2 exists to
    # close.
    account = None
    if any(c.isdigit() for c in typed) and "@" not in typed:
        try:
            account = db.scalar(select(Account).where(Account.phone == normalise_whatsapp(typed)))
        except SignupError:
            # Not a readable Kenyan number — fall through to the email path
            # rather than telling the caller which kind of thing they typed.
            account = None

    if account is None:
        account = db.scalar(select(Account).where(func.lower(Account.email) == typed.lower()))

    # Always hash, even with no account — this is the timing equaliser and must
    # not be optimised away into a conditional.
    stored_hash = account.password_hash if account else DUMMY_HASH
    password_ok = verify_password(password, stored_hash)

    if account is None or not password_ok or not account.is_active:
        raise AuthError("Incorrect WhatsApp number, email, or password.")

    # The only moment the plaintext exists, so the only moment a rehash to
    # stronger parameters is possible.
    if needs_rehash(account.password_hash):
        account.password_hash = hash_password(password)

    account.last_login_at = func.now()
    db.flush()
    return account


def get_account(db: Session, account_id: int) -> Account | None:
    """Load an account by id, or None if it is missing or deactivated."""
    account = db.get(Account, account_id)
    if account is None or not account.is_active:
        return None
    return account


def find_by_phone(db: Session, phone: str) -> Account | None:
    """The account registered to this WhatsApp number, if any."""
    return db.scalar(select(Account).where(Account.phone == phone))


def create_account_for_phone(db: Session, *, phone: str, shop_name: str) -> Account:
    """
    Create a seller from a verified WhatsApp number. No password involved.

    Args:
        db: Session. The caller commits.
        phone: 2547XXXXXXXX, already PROVEN by an OTP. This function does not
            verify anything — it trusts its caller, which is why only the OTP
            route may call it.
        shop_name: Becomes the display name and the basis for the slug.

    Returns:
        The new account, with ``seller`` populated.

    Raises:
        SignupError: On an unusable shop name, or a number already registered.

    Notes:
        THE PASSWORD IS RANDOM AND DISCARDED. ``password_hash`` is NOT NULL, and
        making it nullable would mean every login path has to handle the "no
        password" case forever. A long random value nobody ever learns is
        simpler and strictly safer: the account is unreachable by password until
        the seller deliberately sets one.

        EMAIL IS LEFT EMPTY. A Kenyan seller signing up through WhatsApp has no
        reason to give one, and demanding it is the friction this flow exists to
        remove. It stays available for anyone who wants it later.
    """
    clean_shop = shop_name.strip()
    if not clean_shop:
        raise SignupError("Please enter a shop name.")

    if find_by_phone(db, phone) is not None:
        raise SignupError("That WhatsApp number already has a shop. Try signing in.")

    slug = reserve_slug(db, clean_shop)

    account = Account(
        email=None,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        phone=phone,
        is_active=True,
    )
    account.seller = Seller(slug=slug, display_name=clean_shop, whatsapp_number=phone)

    db.add(account)
    db.flush()
    return account
