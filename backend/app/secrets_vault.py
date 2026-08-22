"""
Reversible encryption for third-party credentials we are trusted to hold.

    seller's Daraja passkey ──▶ encrypt() ──▶ ciphertext (stored)
                                                   │
                             decrypt() ◀───────────┘  only when calling Daraja

WHY THIS IS NOT IN ``security.py``. That module hashes passwords, and hashing is
deliberately **irreversible** — the whole point is that nobody, including us,
can recover the input. This is the opposite operation, and mixing the two in one
file invites the mistake of reaching for the wrong one. A password must never be
encrypted; a Daraja passkey must never be hashed, because we have to send it.

WHY WE HOLD THESE AT ALL. A seller with a Till or Paybill who wants automatic
payment confirmation has to give us their shortcode credentials — Daraja has no
delegated-access model, no OAuth, no scoped token. There is no version of
seller-initiated STK Push that avoids custody.

That is precisely why the manual confirmation path exists and is permanent: a
seller on Pochi la Biashara, or one who simply does not want to hand over
secrets, still sells. **Nobody is forced into this.**

WHAT A BREACH COSTS, stated plainly so it is never treated casually: these
credentials let the holder move money through the seller's shortcode. They are
encrypted at rest, never logged, never rendered into a template, and never
returned by an API. The encryption key lives in the environment, not the
database — a dump of Postgres alone yields nothing.

KEY ROTATION is not implemented and is deliberately called out rather than
quietly omitted. Rotating ``SECRET_KEY`` today invalidates every stored
credential, and the seller must re-enter it. Before this has real users, that
becomes a keyed rotation with a key id per row.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class SecretDecryptionError(Exception):
    """
    Stored ciphertext could not be read back.

    Almost always means ``SECRET_KEY`` changed since the value was written. It
    is raised rather than returning None so a caller cannot mistake "we cannot
    read this credential" for "this seller has no credential" — the first should
    stop a payment attempt loudly, the second is ordinary.
    """


@lru_cache(maxsize=1)
def _cipher() -> Fernet:
    """
    The Fernet cipher, derived from ``SECRET_KEY``.

    Derived rather than configured separately so there is one secret to manage
    in Railway, not two — a second key that nobody remembers to set is a second
    way to lose every stored credential.

    SHA-256 of the configured key gives the exact 32 bytes Fernet requires from
    a secret of any length. Fernet then handles AES-128-CBC with an HMAC, so a
    tampered ciphertext fails loudly instead of decrypting to rubbish.

    Cached because deriving it per call is wasted work on every payment.
    """
    digest = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    """
    Encrypt a credential for storage.

    Args:
        plaintext: The secret, as the seller typed it.

    Returns:
        URL-safe ciphertext, suitable for a text column.
    """
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """
    Read a stored credential back.

    Args:
        ciphertext: What :func:`encrypt` produced.

    Returns:
        The original secret.

    Raises:
        SecretDecryptionError: If the value cannot be decrypted or has been
            tampered with. Never returns a partial or default value — a payment
            must fail visibly rather than proceed with a wrong credential.
    """
    try:
        return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError(
            "Stored credential could not be decrypted. This usually means "
            "SECRET_KEY changed since it was saved."
        ) from exc
