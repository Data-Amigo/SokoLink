"""
Proving a seller controls the social account they claim.

    connect  ──> generate code ──> seller pastes it into their bio
                                              │
    verify   ──> scrape profile ──> bio contains code? ──> verified_at set
                                              │
                                    seller removes the code

WHY THIS EXISTS: without proof, a handle is a string someone typed. A stranger
could claim another seller's account, have us scrape her videos and her photos,
and publish a storefront pointing at THEIR WhatsApp number. The buyer sees
nothing wrong. That is sales diversion, and one incident reaching Kenyan seller
groups would be very hard to recover from.

WHY BIO-CODE RATHER THAN OAUTH: OAuth is better — one tap, and the platform
vouches for them. It is unavailable until TikTok and Meta approve our app,
which runs on their clock, and P1 sellers arrive before that. Bio-code needs no
approval from anyone and uses a field we already fetch (spike 02 confirmed
`authorMeta.signature` comes back populated).

The same pattern Google Search Console uses, so sellers will not find it alien.

WHAT IT DOES NOT PROVE: someone with temporary access to the account could pass
it. It defeats the realistic attack — a stranger typing a handle they have
never touched — and that is the bar it is built to clear.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from app.models import SocialAccount, VerificationMethod
from app.services.scraper import ScraperEngine, ScraperError

#: Alphabet with the ambiguous characters removed. A seller retypes this on a
#: phone keyboard, and 0/O and 1/I/l are where that goes wrong.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

CODE_LENGTH = 6

#: Prefixed so the seller can see at a glance what the string is for, and so we
#: can find it in a bio full of hashtags and phone numbers.
CODE_PREFIX = "soko-"

#: Long enough to switch apps, edit a bio and come back — including on a slow
#: connection. Short enough that an abandoned attempt cannot be resumed months
#: later by whoever controls the account by then.
CODE_LIFETIME = timedelta(hours=24)


class VerificationError(Exception):
    """
    Verification could not be completed.

    Messages here are shown to the seller, so they say what to do next rather
    than what went wrong internally.
    """


def generate_code() -> str:
    """
    Mint a one-time verification code.

    Returns:
        Something like ``soko-K7M2QP``. Unambiguous on a phone keyboard, and
        recognisable inside a bio full of hashtags.
    """
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{CODE_PREFIX}{body}"


def start_verification(account: SocialAccount) -> str:
    """
    Issue a fresh code for an account and return it for display.

    Always issues a NEW code rather than reusing an outstanding one: a seller
    asking again usually means the previous code was lost, and reissuing costs
    nothing while reusing extends a live code's lifetime indefinitely.

    Args:
        account: The account being claimed. The caller commits.

    Returns:
        The code to show the seller.

    Raises:
        VerificationError: If the account is already verified — reissuing would
            invite a downgrade, and there is nothing to prove.
    """
    if account.is_verified:
        raise VerificationError("This account is already verified.")

    code = generate_code()
    account.verification_code = code
    account.verification_expires_at = datetime.now(UTC) + CODE_LIFETIME
    return code


def check_verification(account: SocialAccount, scraper: ScraperEngine) -> bool:
    """
    Re-read the account's bio and look for the outstanding code.

    Args:
        account: The account being claimed, with a code already issued.
        scraper: Engine used to re-fetch the profile. One paid call.

    Returns:
        True when the code was found and the account is now verified.

    Raises:
        VerificationError: If no code was issued, the code expired, or the
            profile could not be read. Each message tells the seller what to do.

    Notes:
        On success the code is CLEARED. A live code lying around is a standing
        target, and it has done its job.
    """
    if not account.verification_code:
        raise VerificationError("No verification in progress. Start again to get a code.")

    if account.verification_expires_at is not None:
        expires = account.verification_expires_at
        # Rows written before a timezone-aware column existed can come back
        # naive; treat those as UTC rather than crashing on the comparison.
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires < datetime.now(UTC):
            raise VerificationError("That code has expired. Start again to get a new one.")

    try:
        profile = scraper.fetch_profile(account.handle, limit=1)
    except ScraperError as exc:
        # Surfaced rather than swallowed: a private profile and a rate limit
        # need different responses from the seller.
        raise VerificationError(f"Could not read the @{account.handle} profile: {exc}") from exc

    bio = profile.author.bio or ""

    # Case-insensitive: sellers retype the code, and phone keyboards
    # auto-capitalise. Rejecting "SOKO-K7M2QP" would be needless friction on a
    # step that is already asking a favour.
    if account.verification_code.lower() not in bio.lower():
        return False

    account.verified_at = datetime.now(UTC)
    account.verification_method = VerificationMethod.BIO_CODE.value

    # Job done — leaving it live would be a standing target.
    account.verification_code = None
    account.verification_expires_at = None
    return True


def verify_via_oauth(account: SocialAccount, platform_handle: str) -> None:
    """
    Record ownership proven by the platform itself.

    Called once TikTok or Meta approve our app. The handle comes from the
    provider's own token response, never from anything the seller typed — that
    is the entire value of OAuth.

    Args:
        account: The account being claimed.
        platform_handle: The handle the PROVIDER reported.

    Raises:
        VerificationError: If the provider's handle does not match the claim,
            which means the seller authenticated as somebody else.
    """
    if platform_handle.strip().lstrip("@").lower() != account.handle:
        raise VerificationError(
            f"You signed in as @{platform_handle}, but this shop claims "
            f"@{account.handle}. Sign in with the matching account."
        )

    account.verified_at = datetime.now(UTC)
    account.verification_method = VerificationMethod.OAUTH.value
    account.verification_code = None
    account.verification_expires_at = None


def require_syncable(account: SocialAccount) -> None:
    """
    Refuse to sync an account we have no right to.

    THE RAIL. Every sync path calls this first. Without it, an unproven claim
    would produce a full catalogue of somebody else's products.

    Args:
        account: The account about to be scraped.

    Raises:
        VerificationError: If the account is disconnected or unverified.
    """
    if not account.is_active:
        raise VerificationError(f"@{account.handle} is disconnected. Reconnect it to sync.")
    if not account.is_verified:
        raise VerificationError(
            f"@{account.handle} is not verified yet. Verify that you own this "
            "account before importing its products."
        )
