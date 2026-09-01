"""
Inbound WhatsApp — the endpoint Twilio posts to.

    POST /webhooks/whatsapp   Twilio delivers an inbound message here. PUBLIC.

WHAT IT DOES TODAY, AND WHAT IT DOES NOT. It verifies the request really came
from Twilio, records the message so a redelivery cannot be processed twice, and
replies. It does NOT yet parse a forwarded catalogue into products — that is the
next milestone. Standing this up first means the URL can be registered with
Twilio, the signature check can be proven against real traffic, and the parsing
work lands on a path already known to work.

SIGNATURE VERIFICATION IS NOT OPTIONAL HERE. This URL is public and it will act
on what it is told. Twilio signs every request with HMAC-SHA1 over the full URL
plus the sorted POST fields, keyed by the account's auth token — so a forged
"seller X sent this product" is rejected before it is read. Unlike Daraja, which
signs nothing, there is no excuse for skipping it.

IDEMPOTENCY, BECAUSE TWILIO REDELIVERS. Any webhook that is slow, errors, or
times out is retried. ``MessageSid`` is unique per message, so a second delivery
of the same SID is recognised and ignored rather than creating a second product
from one forwarded photo.

IT ALWAYS ANSWERS 200 ONCE IT HAS THE MESSAGE. Twilio retries anything else, and
a retry cannot fix "we already have this". The one case that DOES return an
error is a failure before the message is safely recorded, where a retry is
exactly what we want.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.config import get_settings
from app.db import get_db
from app.models import WaMessage
from app.services.bot import Reply, handle
from app.services.intake import MediaFetch, download_media

router = APIRouter(tags=["webhooks"])

#: Where Twilio posts. Fixed, because it is registered in their console and a
#: silent change would strand every inbound message.
WHATSAPP_WEBHOOK_PATH = "/webhooks/whatsapp"

#: Twilio's empty TwiML: "received, say nothing back".
_NO_REPLY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _twiml(replies: Sequence[Reply]) -> str:
    """
    Wrap the bot's replies in TwiML so Twilio sends them back.

    Replying in the webhook RESPONSE rather than through the REST API means no
    second network call, no API credentials on this path, and no way to message
    somebody who did not just message us. It also needs no content template and
    no Meta approval, which is what lets the whole shop run on the sandbox.

    Args:
        replies: In order. Twilio sends them as separate messages.

    Returns:
        A TwiML document.

    Notes:
        EVERYTHING IS XML-ESCAPED. A product called "Tom & Jerry" would
        otherwise produce a malformed document that Twilio drops silently —
        which looks exactly like the webhook never running.
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?><Response>']
    for reply in replies:
        parts.append("<Message>")
        parts.append(f"<Body>{escape(reply.body)}</Body>")
        if reply.media_url:
            parts.append(f"<Media>{escape(reply.media_url)}</Media>")
        parts.append("</Message>")
    parts.append("</Response>")
    return "".join(parts)


def _expected_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    """
    Recompute Twilio's ``X-Twilio-Signature`` for this exact request.

    Their scheme: take the full URL, append each POST field as ``key + value``
    in KEY-SORTED order, HMAC-SHA1 it with the auth token, base64 the digest.
    Every part matters — a different URL, a different order, or a missing field
    all produce a different signature.

    Args:
        url: The absolute URL Twilio was configured with, exactly.
        params: The POST form fields.
        auth_token: The Twilio auth token, which is the shared secret.

    Returns:
        The expected signature, to be compared in constant time.
    """
    payload = url
    for key in sorted(params):
        payload += key + params[key]

    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


@router.post(WHATSAPP_WEBHOOK_PATH)
async def whatsapp_inbound(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """
    Receive one inbound WhatsApp message from Twilio.

    Returns:
        Empty TwiML with 200 once the message is recorded — including for a
        redelivery, which is normal rather than an error.

    Raises:
        HTTPException: 403 when the signature does not verify, 500 when the body
            cannot be read. The first must not be retried; the second should be.
    """
    settings = get_settings()
    if not settings.twilio_auth_token:
        # Refusing beats accepting unverifiable traffic on a public endpoint
        # that will later create products from what it is told.
        raise HTTPException(status_code=503, detail="WhatsApp is not configured")

    try:
        form = await request.form()
    except Exception as exc:  # noqa: BLE001 — any parse failure means "retry me"
        raise HTTPException(status_code=500, detail="Unreadable webhook body") from exc

    params = {key: str(value) for key, value in form.items()}

    # THE URL MUST BE THE ONE TWILIO WAS GIVEN, not the one we happen to see.
    # Behind Railway's proxy the request arrives as http on an internal host,
    # and signing that would never match. APP_BASE_URL is the public identity.
    signed_url = f"{settings.app_base_url}{WHATSAPP_WEBHOOK_PATH}"
    expected = _expected_signature(signed_url, params, settings.twilio_auth_token)
    provided = request.headers.get("X-Twilio-Signature", "")

    # compare_digest, not ==: a plain comparison returns early on the first
    # wrong byte, and that timing difference is enough to forge a signature one
    # byte at a time.
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="Bad signature")

    message_sid = params.get("MessageSid") or params.get("SmsMessageSid") or ""
    if not message_sid:
        # Nothing to deduplicate on. Acknowledge rather than invite a retry
        # that would arrive equally unidentifiable.
        return PlainTextResponse(_NO_REPLY, media_type="application/xml")

    already = db.scalar(select(WaMessage).where(WaMessage.provider_message_id == message_sid))
    if already is not None:
        # A redelivery. Twilio retries anything slow, and the whole point of
        # recording the SID is that the second arrival changes nothing.
        return PlainTextResponse(_NO_REPLY, media_type="application/xml")

    phone = params.get("From", "").replace("whatsapp:", "").lstrip("+")

    db.add(
        WaMessage(
            provider_message_id=message_sid,
            # Twilio prefixes numbers with the channel: whatsapp:+254712345678.
            # Stored bare so it matches Account.phone and Seller.whatsapp_number.
            from_number=phone,
            body=params.get("Body") or None,
            media_count=int(params.get("NumMedia") or 0),
            raw=params,
        )
    )

    # THE BOT RUNS ONLY ON FIRST RECEIPT. The redelivery branch above returns
    # early, so a Twilio retry can never add the same item to a basket twice or
    # place a second order from one message.
    # Twilio numbers attachments: MediaUrl0, MediaContentType0, and so on.
    # A forwarded catalogue post is often several photos in one message, which
    # is why this is a list and not a single optional field.
    # The fetch is bound here, where we know this is Twilio. `url=url` binds the
    # value per iteration — a bare closure over the loop variable would give
    # every attachment the LAST url, which is a classic and silent way to parse
    # the same photo N times.
    def _twilio_fetch(url: str) -> MediaFetch:
        """Bind ONE url into a fetch. A bare closure over a loop variable would
        give every attachment the last url — the same photo parsed N times."""
        return lambda: download_media(url)

    media: list[tuple[str, MediaFetch]] = [
        (f"{message_sid}:{index}", _twilio_fetch(params[f"MediaUrl{index}"]))
        for index in range(int(params.get("NumMedia") or 0))
        if params.get(f"MediaUrl{index}")
    ]

    outcome = handle(db, phone, params.get("Body") or "", media=media)

    # One commit for the message record AND everything the bot did. A reply
    # promising "added to your basket" must not survive a failed basket write.
    db.commit()

    # ``outcome.notify`` IS DROPPED HERE, and that is a known limit of the
    # Twilio path rather than an oversight. A TwiML response can only answer the
    # person who wrote in; reaching a third party needs a separate REST call
    # with its own credentials and failure modes. Meta is the live provider and
    # its webhook sends them — see api/whatsapp_cloud.py.
    #
    # The consequence, if a shop ever runs on Twilio again: order alerts and
    # buyer receipts go unsent. Nothing is lost, because `orders` remains the
    # system of record, but nobody's phone rings.
    return PlainTextResponse(_twiml(outcome.replies), media_type="application/xml")
