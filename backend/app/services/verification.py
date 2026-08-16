"""
Proving a seller controls the social account they claim.

    start_claim   ──> AccountClaim + code ──> seller pastes code into their bio
                          (proves nothing)              │
                                                        ▼
    check_claim   ──> re-read bio ──> match? ──> SocialAccount CREATED
                                          │           claim deleted
                                         no ──> still just a claim

THE RULE, and it is structural rather than remembered:

    **A SocialAccount row only ever exists once ownership is proven.**

``SocialAccount.verified_at`` is NOT NULL, so the database cannot hold an
unproven connection. Nothing downstream has to filter for verified accounts,
because there is no other kind. A failed or abandoned claim leaves a row in
``account_claims`` that grants nothing and expires on its own.

WHY THIS MATTERS: without proof, a handle is a string someone typed. A stranger
could claim another seller's account, have us scrape her videos and her photos,
and publish a storefront pointing at THEIR WhatsApp number. The buyer sees
nothing wrong. That is sales diversion, and one incident reaching Kenyan seller
groups would be very hard to come back from.

WHY BIO-CODE AND NOT OAUTH, YET: OAuth is better — one tap, and the platform
vouches for them, with no code to copy. It is unavailable until TikTok and Meta
approve OUR app, which runs on their clock; reading a seller's posts needs a
second scope with its own approval. P1 sellers arrive before either lands.
:func:`complete_via_oauth` is written and tested so the switch is a call-site
change.

WHAT BIO-CODE DOES NOT PROVE: someone with temporary access to the account could
pass it. It defeats the realistic attack — a stranger typing a handle they have
never touched — and that is the bar it is built to clear.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountClaim, Platform, SocialAccount, VerificationMethod
from app.models.account_claim import MAX_ATTEMPTS
from app.schemas.tiktok import TikTokAuthor
from app.services.scraper import ScraperEngine, ScraperError

#: Alphabet with the ambiguous characters removed. A seller retypes this on a
#: phone keyboard, and 0/O and 1/I/l are where that goes wrong.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

CODE_LENGTH = 6

#: Prefixed so the seller can see at a glance what the string is for, and so we
#: can find it in a bio full of hashtags and phone numbers.
CODE_PREFIX = "soko-"

#: Long enough to switch apps, edit a bio and come back on a slow connection.
#: Short enough that an abandoned claim cannot be resumed months later by
#: whoever controls that account by then.
CLAIM_LIFETIME = timedelta(hours=24)


class VerificationError(Exception):
    """
    Verification could not proceed.

    Messages are shown to the seller, so they say what to do next rather than
    what went wrong internally.
    """


def generate_code() -> str:
    """
    Mint a one-time verification code.

    Returns:
        Something like ``soko-K7M2QP`` — unambiguous on a phone keyboard, and
        recognisable inside a bio full of hashtags and phone numbers.
    """
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"{CODE_PREFIX}{body}"


def start_claim(db: Session, seller_id: int, platform: Platform | str, handle: str) -> AccountClaim:
    """
    Begin proving ownership of an account. Connects nothing.

    Args:
        db: Session. The caller commits.
        seller_id: Who is claiming.
        platform: Which platform.
        handle: The handle being claimed, with or without a leading @.

    Returns:
        The claim, carrying the code to show the seller.

    Raises:
        VerificationError: If the handle is unusable, already connected by this
            seller, or already proven by somebody else.
    """
    clean = handle.strip().lstrip("@").lower()
    if not clean:
        raise VerificationError("Please enter the account handle.")

    platform_value = str(platform)

    # Someone else proved this handle first. Say so plainly: the alternative is
    # a seller retyping a handle forever with no idea why it never takes.
    taken = db.scalar(
        select(SocialAccount).where(
            SocialAccount.platform == platform_value,
            SocialAccount.handle == clean,
        )
    )
    if taken is not None:
        if taken.seller_id == seller_id:
            raise VerificationError(f"@{clean} is already connected to your shop.")
        raise VerificationError(
            f"@{clean} has already been verified by another Biashara Mall shop. "
            "If this is your account, contact support."
        )

    # Replace any claim this seller had in flight for this platform — asking
    # again almost always means the previous code was lost, and reusing one
    # would extend a live code's life indefinitely.
    existing = db.scalar(
        select(AccountClaim).where(
            AccountClaim.seller_id == seller_id,
            AccountClaim.platform == platform_value,
        )
    )
    if existing is not None:
        db.delete(existing)
        db.flush()

    claim = AccountClaim(
        seller_id=seller_id,
        platform=platform_value,
        handle=clean,
        code=generate_code(),
        expires_at=datetime.now(UTC) + CLAIM_LIFETIME,
    )
    db.add(claim)
    db.flush()
    return claim


def check_claim(db: Session, claim: AccountClaim, scraper: ScraperEngine) -> SocialAccount | None:
    """
    Re-read the profile and, if the code is there, connect the account.

    Args:
        db: Session. The caller commits.
        claim: The pending claim.
        scraper: Engine used to re-fetch the profile. Costs one paid call.

    Returns:
        The newly created SocialAccount on success, or None when the code was
        not found — which is an ordinary outcome, not an error. The seller
        probably has not saved their bio yet.

    Raises:
        VerificationError: If the claim expired, exhausted its attempts, the
            profile could not be read, or the handle was verified by someone
            else while this claim was open.
    """
    if claim.is_expired:
        raise VerificationError("That code has expired. Start again to get a new one.")

    # Checked BEFORE the scrape: each attempt is billable, and a seller
    # hammering Verify should not be able to spend our credit.
    if claim.attempts_exhausted:
        raise VerificationError(
            f"Too many attempts ({MAX_ATTEMPTS}). Start again to get a new code."
        )

    claim.attempts += 1

    try:
        profile = scraper.fetch_profile(claim.handle, limit=1)
    except ScraperError as exc:
        # Surfaced, not swallowed: a private profile and a rate limit need
        # different responses from the seller.
        raise VerificationError(f"Could not read the @{claim.handle} profile: {exc}") from exc

    bio = profile.author.bio or ""

    # Case-insensitive: sellers retype the code and phone keyboards
    # auto-capitalise. Rejecting "SOKO-K7M2QP" would be needless friction on a
    # step that is already asking a favour.
    if claim.code.lower() not in bio.lower():
        return None

    return _connect(
        db,
        claim=claim,
        author=profile.author,
        method=VerificationMethod.BIO_CODE,
    )


def complete_via_oauth(
    db: Session, claim: AccountClaim, provider_handle: str, author: TikTokAuthor | None = None
) -> SocialAccount:
    """
    Connect an account whose ownership the platform itself vouched for.

    Called once TikTok or Meta approve our app. ``provider_handle`` comes from
    the provider's token response, never from anything the seller typed — that
    is the entire value of OAuth.

    Args:
        db: Session. The caller commits.
        claim: The pending claim.
        provider_handle: The handle the PROVIDER reported.
        author: Profile details, if the provider returned them.

    Returns:
        The newly created SocialAccount.

    Raises:
        VerificationError: If the provider's handle does not match the claim,
            meaning the seller authenticated as a different account.
    """
    if provider_handle.strip().lstrip("@").lower() != claim.handle:
        raise VerificationError(
            f"You signed in as @{provider_handle}, but this shop claims "
            f"@{claim.handle}. Sign in with the matching account."
        )

    return _connect(db, claim=claim, author=author, method=VerificationMethod.OAUTH)


def _connect(
    db: Session,
    *,
    claim: AccountClaim,
    author: TikTokAuthor | None,
    method: VerificationMethod,
) -> SocialAccount:
    """
    Turn a proven claim into a connected account, and discard the claim.

    The only function in the codebase that creates a SocialAccount. Keeping it
    single means the "must be verified" invariant has exactly one place to be
    got right.

    Raises:
        VerificationError: If another seller verified this handle while the
            claim was open — a genuine race, and the loser must be told.
    """
    account = SocialAccount(
        seller_id=claim.seller_id,
        platform=claim.platform,
        handle=claim.handle,
        verified_at=datetime.now(UTC),
        verification_method=method.value,
    )

    # Profile details auto-fill so the seller confirms rather than types.
    if author is not None:
        account.display_name = author.display_name
        account.avatar_url = author.avatar_url
        account.bio = author.bio
        account.follower_count = author.follower_count
        account.post_count = author.video_count

    db.add(account)
    db.delete(claim)

    try:
        db.flush()
    except Exception as exc:  # noqa: BLE001 — re-raised with a seller-facing message
        db.rollback()
        raise VerificationError(
            f"@{claim.handle} was just verified by another shop. "
            "If this is your account, contact support."
        ) from exc

    return account


def require_syncable(account: SocialAccount) -> None:
    """
    Refuse to sync a disconnected account.

    Verification is no longer checked here — it cannot fail. An account that
    exists in ``social_accounts`` is verified by construction, because the
    column is NOT NULL and :func:`_connect` is the only thing that writes one.

    Raises:
        VerificationError: If the seller has disconnected the account.
    """
    if not account.is_active:
        raise VerificationError(f"@{account.handle} is disconnected. Reconnect it to sync.")


def purge_expired_claims(db: Session) -> int:
    """
    Delete claims that can no longer be completed.

    Housekeeping. Expired claims grant nothing, but they hold the
    (seller, platform) slot, so a seller who abandoned an attempt would
    otherwise be blocked from starting a fresh one.

    Returns:
        How many were removed.
    """
    stale = db.scalars(
        select(AccountClaim).where(AccountClaim.expires_at < datetime.now(UTC))
    ).all()
    for claim in stale:
        db.delete(claim)
    db.flush()
    return len(stale)
