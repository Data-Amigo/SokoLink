"""
Signing in with a WhatsApp number and a one-time code.

    request_code(phone) ──▶ LoginCode(hashed) ──▶ WhatsApp message
                                    │
    verify_code(phone, digits) ─────┘──▶ the phone is proven ──▶ session

WHY A CODE RATHER THAN A PASSWORD. The number is the thing a Kenyan seller
actually remembers, it is already how their customers reach them, and WhatsApp
can prove they hold it. A password is a second secret to invent, forget and
reset — for an audience whose entire business runs in one app.

WHY THIS WORKS BEFORE THE BOT EXISTS. Sending a WhatsApp message needs no
webhook; only receiving does. We send, the seller types the code into a web
page, and nothing has to reach us from Meta.

SIX DIGITS IS NOTHING WITHOUT THE RAILS. One in a million per guess is weak if
guesses are unlimited. Three things make it safe, and removing any one of them
breaks it:

    MAX_ATTEMPTS      a code dies after a few wrong guesses
    CODE_TTL          it dies on its own within minutes
    RESEND_COOLDOWN   a number cannot be flooded, by us or by an attacker

THE CODE IS NEVER RETURNED, LOGGED OR RENDERED. It exists in memory long enough
to be hashed and sent, and nowhere else. ``request_code`` returns nothing.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LoginCode
from app.security import hash_password, verify_password
from app.services.messaging import MessagingError, Messenger

#: Long enough to switch apps, read a message and switch back; short enough
#: that a code seen over a shoulder is worthless by the time it is used.
CODE_TTL = timedelta(minutes=10)

#: Wrong guesses before the code dies. Five is generous for a human typing six
#: digits and hopeless for anyone guessing.
MAX_ATTEMPTS = 5

#: A new code cannot be requested until this has passed. Protects the seller's
#: phone from being buried, and us from paying to send it.
RESEND_COOLDOWN = timedelta(seconds=60)

_CODE_DIGITS = 6


class OtpError(Exception):
    """A code could not be sent or accepted, with a message safe to show."""


def _new_code() -> str:
    """
    Six cryptographically random digits.

    ``secrets``, not ``random``: the latter is seeded predictably and its output
    is reconstructable from a few samples, which for a login code means an
    attacker who watches a handful can compute the next one.
    """
    return f"{secrets.randbelow(10**_CODE_DIGITS):0{_CODE_DIGITS}d}"


def _live_code(db: Session, phone: str) -> LoginCode | None:
    """The most recent code for this number, whatever its state."""
    return db.scalar(
        select(LoginCode).where(LoginCode.phone == phone).order_by(LoginCode.created_at.desc())
    )


def request_code(db: Session, phone: str, messenger: Messenger) -> None:
    """
    Issue a code and send it to the seller's WhatsApp.

    Args:
        db: Session.
        phone: 2547XXXXXXXX, already normalised by the caller.
        messenger: The WhatsApp seam. A fake in tests; nothing is sent there.

    Raises:
        OtpError: If a code was sent too recently, or WhatsApp refused.

    Notes:
        RETURNS NOTHING, ON PURPOSE. Handing the code back to the caller is how
        it ends up in a log line, a template context or a test fixture that
        someone later copies into production code.

        THE ROW IS WRITTEN BEFORE THE SEND. If WhatsApp fails we raise, and the
        unused row simply expires — which is the safe direction. Sending first
        and failing to record would give a seller a code that can never work.
    """
    previous = _live_code(db, phone)
    if previous is not None and not previous.is_spent:
        age = datetime.now(UTC) - previous.created_at.replace(tzinfo=UTC)
        if age < RESEND_COOLDOWN:
            wait = int((RESEND_COOLDOWN - age).total_seconds())
            raise OtpError(f"We just sent a code. Try again in {wait} seconds.")

    code = _new_code()
    db.add(
        LoginCode(
            phone=phone,
            code_hash=hash_password(code),
            expires_at=datetime.now(UTC) + CODE_TTL,
        )
    )
    db.flush()

    minutes = int(CODE_TTL.total_seconds() // 60)
    try:
        messenger.send(
            phone,
            f"{code} is your Biasharamall code. It expires in {minutes} minutes. "
            "If you did not ask for it, ignore this message.",
        )
    except MessagingError as exc:
        raise OtpError(str(exc)) from exc


def verify_code(db: Session, phone: str, code: str) -> bool:
    """
    Check a code and spend it.

    Args:
        db: Session.
        phone: The number the code was sent to.
        code: What the seller typed.

    Returns:
        True when the code was correct, live and unspent. False otherwise —
        the caller decides what to say, and should say the same thing for every
        kind of failure.

    Raises:
        OtpError: When there is nothing to check, or the attempt cap is hit.
            These are distinguishable from "wrong digits" because the seller
            needs different advice: request a new code, rather than retype.

    Notes:
        A WRONG GUESS COSTS AN ATTEMPT EVEN IF THE CODE HAS EXPIRED. Counting
        only live codes would let an attacker guess freely against an expired
        one and learn nothing is being counted.
    """
    record = _live_code(db, phone)

    if record is None or record.is_spent:
        raise OtpError("Request a new code.")

    if record.attempts >= MAX_ATTEMPTS:
        raise OtpError("Too many attempts. Request a new code.")

    record.attempts += 1
    db.flush()

    if record.is_expired:
        raise OtpError("That code has expired. Request a new one.")

    if not verify_password(code.strip(), record.code_hash):
        return False

    record.consumed_at = datetime.now(UTC)
    db.flush()
    return True
