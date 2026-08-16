"""
Password hashing and session tokens.

    password ──> Argon2id ──> hash (stored)
    account  ──> JWT (signed with SECRET_KEY) ──> session cookie

WHY Argon2id and not bcrypt: it is the current password-hashing recommendation
and is memory-hard, which makes GPU cracking far more expensive. The library
handles salting and parameter encoding, so a hash carries its own parameters and
can be upgraded later without a flag day.

WHY the verify path is written the way it is: authentication must not leak
WHICH half of a credential pair was wrong. A "no such email" that returns
instantly and a "wrong password" that takes 50ms is an account-enumeration
oracle a stranger can measure. :func:`verify_password` is therefore always
called, even when no account was found — see ``DUMMY_HASH``.

Nothing outside this module hashes, compares, or signs. If a password reaches
any other file, something is wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import settings

_hasher = PasswordHasher()

#: A real Argon2 hash of a value nobody knows, used to burn the same CPU time
#: when an email does not exist as when it does. Without this, response timing
#: tells an attacker which emails are registered.
DUMMY_HASH = _hasher.hash("biashara-timing-equaliser-not-a-real-password")

#: How long a session lasts. Long enough that a seller reviewing drafts on a
#: slow connection is not logged out mid-edit; short enough that a borrowed
#: laptop is not a standing invitation.
SESSION_LIFETIME = timedelta(days=14)

ALGORITHM = "HS256"

#: The cookie the browser carries. HttpOnly, so no script can read it.
SESSION_COOKIE = "biashara_session"

#: Minimum password length. Length beats character-class rules — a long
#: passphrase is both stronger and easier for a seller on a phone keyboard.
MIN_PASSWORD_LENGTH = 8


class AuthError(Exception):
    """
    Authentication failed.

    Deliberately carries no detail about WHY. The caller renders one message
    for every failure, because "no such account" and "wrong password" must be
    indistinguishable from outside.
    """


def hash_password(password: str) -> str:
    """
    Hash a password for storage.

    Args:
        password: The plaintext, already length-validated by the caller.

    Returns:
        An Argon2id hash, carrying its own parameters and salt.
    """
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """
    Check a password against a stored hash.

    Args:
        password: The plaintext submitted at login.
        password_hash: The stored hash, or ``DUMMY_HASH`` when no account was
            found — so the timing of both paths matches.

    Returns:
        True only on a genuine match.
    """
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """
    Whether a stored hash used weaker parameters than we now use.

    Lets us strengthen hashing over time: on a successful login the password is
    briefly in memory, which is the only moment a rehash is possible.
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        # An unparseable hash cannot be verified against anyway; force a rehash
        # rather than pretending it is current.
        return True


def create_session_token(account_id: int) -> str:
    """
    Mint a signed session token.

    Args:
        account_id: The account the session belongs to.

    Returns:
        A signed JWT carrying the account id and an expiry.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(account_id),
        "iat": now,
        "exp": now + SESSION_LIFETIME,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def read_session_token(token: str) -> int:
    """
    Verify a session token and return whose it is.

    Args:
        token: The value from the session cookie.

    Returns:
        The account id.

    Raises:
        AuthError: If the token is expired, tampered with, or malformed. All
            three are one failure from the caller's point of view.
    """
    try:
        payload: dict[str, Any] = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid or expired session") from exc

    subject = payload.get("sub")
    if not subject:
        raise AuthError("Session token carries no subject")

    try:
        return int(subject)
    except (TypeError, ValueError) as exc:
        raise AuthError("Session token subject is not an account id") from exc


def validate_password_strength(password: str) -> None:
    """
    Reject passwords too weak to store.

    Length only, deliberately. Character-class rules push people toward
    "Passw0rd!" while a long phrase is both stronger and far easier to type on
    a phone.

    Raises:
        ValueError: With a message safe to show the user.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
