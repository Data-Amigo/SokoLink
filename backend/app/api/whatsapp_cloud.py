"""
Meta's WhatsApp Cloud API webhook.

    GET  /webhooks/meta   Meta's one-time verification handshake
    POST /webhooks/meta   inbound messages. PUBLIC.

WHY THIS IS A SECOND WEBHOOK RATHER THAN A CHANGE TO THE FIRST. Twilio and Meta
share nothing at this layer: form-encoded versus JSON, HMAC-SHA1 over sorted
fields versus HMAC-SHA256 over the raw body, and a reply that rides the HTTP
response versus a reply that is a separate authenticated call. Detecting the
provider by content-type on one path would be a guess in the middle of a
security check. Two paths, each obvious, and the Twilio one is deleted whole
when it retires.

THE GET IS NOT OPTIONAL AND HAS NO TWILIO EQUIVALENT. Meta will not save a
callback URL until it has GET it with a challenge and been echoed the value
back verbatim. Building only the POST — which is what a Twilio-shaped webhook
gives you — means the URL can never be registered at all, and the symptom is a
404 or a 405 in the Meta dashboard with no explanation.

THE SIGNATURE IS OVER THE RAW BODY. It must be computed on the exact bytes
received, before any JSON parsing: re-serialising the decoded payload changes
key order and whitespace, and the digest no longer matches. This is the single
most common way a correct-looking implementation rejects every real message.

IT ALWAYS ANSWERS 200 ONCE THE MESSAGE IS SAFE. Meta retries non-200s and will
disable a webhook that keeps failing. A message we have already seen, a status
callback, a shape we do not recognise — all are 200 and ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from app.config import get_settings
from app.db import get_db
from app.models import WaMessage
from app.services.bot import handle
from app.services.intake import MediaFetch
from app.services.whatsapp_cloud import (
    CloudApiError,
    download_media,
    extract_messages,
    read_message,
    send_buttons,
    send_cta_url,
    send_image,
    send_list,
    send_template,
    send_text,
)

router = APIRouter(tags=["webhooks"])

#: Where Meta posts. Registered in the Meta dashboard, so a silent change here
#: strands every inbound message.
META_WEBHOOK_PATH = "/webhooks/meta"


@router.get(META_WEBHOOK_PATH)
def verify(request: Request) -> Response:
    """
    Answer Meta's verification handshake.

    Meta GETs this URL once, when the callback is saved, with
    ``hub.mode=subscribe``, the verify token you typed into their form, and a
    random ``hub.challenge``. Echo the challenge back as PLAIN TEXT and the URL
    is accepted; anything else and it is rejected with no useful message.

    Returns:
        The challenge, verbatim, as ``text/plain``.

    Raises:
        HTTPException: 403 when the token does not match, 503 when no token is
            configured. Never echoes the challenge in either case — doing so
            would let anybody register our URL against their own app.

    Notes:
        THE CHALLENGE IS RETURNED AS TEXT, NOT JSON. Meta compares the response
        body byte for byte, so a quoted JSON string fails the comparison while
        looking correct in a browser.
    """
    settings = get_settings()
    expected = settings.whatsapp_verify_token
    if not expected:
        raise HTTPException(status_code=503, detail="WHATSAPP_VERIFY_TOKEN is not set")

    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    # compare_digest, not ==: the token is a shared secret and a byte-by-byte
    # early return leaks it one character at a time.
    if mode != "subscribe" or not hmac.compare_digest(token or "", expected):
        raise HTTPException(status_code=403, detail="Verification failed")

    return PlainTextResponse(challenge or "")


def _signature_ok(raw: bytes, header: str | None, app_secret: str) -> bool:
    """
    Whether this body really came from Meta.

    Args:
        raw: The EXACT bytes received. Not the re-serialised payload — key
            order and whitespace would differ and every digest would fail.
        header: The ``X-Hub-Signature-256`` header, ``sha256=<hex>``.
        app_secret: The app secret, which is the shared key.

    Returns:
        True when the digest matches.
    """
    if not header or not header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def _fetch_for(media_id: str) -> MediaFetch:
    """Bind ONE media id into a fetch, so a loop cannot share the last one."""
    return lambda: download_media(media_id)


@router.post(META_WEBHOOK_PATH)
async def receive(request: Request, db: Session = Depends(get_db)) -> Response:
    """
    Receive inbound WhatsApp messages from Meta.

    Returns:
        200 with a tiny JSON body once every message is recorded — including
        for redeliveries and for payloads carrying no messages at all.

    Raises:
        HTTPException: 403 on a bad signature, 503 when unconfigured. Both are
            deliberate: this endpoint acts on what it is told.

    Notes:
        REPLIES ARE SENT, NOT RETURNED. Meta has no TwiML equivalent, so the
        bot's replies go out as separate authenticated calls after the message
        is committed. A send that fails must NOT fail the webhook — the message
        is already recorded, and a non-200 would have Meta redeliver it and run
        the whole conversation step twice.
    """
    settings = get_settings()
    app_secret = settings.whatsapp_app_secret
    if not app_secret:
        raise HTTPException(status_code=503, detail="WHATSAPP_APP_SECRET is not set")

    raw = await request.body()
    if not _signature_ok(raw, request.headers.get("X-Hub-Signature-256"), app_secret):
        raise HTTPException(status_code=403, detail="Bad signature")

    try:
        payload: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        # Signed but unparseable. Acknowledge: a retry would arrive equally
        # unparseable and Meta disables webhooks that keep failing.
        return JSONResponse({"status": "ignored"})

    outgoing: list[tuple[str, list[Any]]] = []

    for message in extract_messages(payload):
        message_id, sender, text, media = read_message(message)
        if not message_id or not sender:
            continue

        already = db.scalar(select(WaMessage).where(WaMessage.provider_message_id == message_id))
        if already is not None:
            # A redelivery. Meta retries anything slow, and the whole point of
            # recording the id is that the second arrival changes nothing.
            continue

        db.add(
            WaMessage(
                provider_message_id=message_id,
                from_number=sender,
                body=text or None,
                media_count=len(media),
                raw=payload,
            )
        )

        fetches: list[tuple[str, MediaFetch]] = [
            (media_id, _fetch_for(media_id)) for media_id, _ in media
        ]
        outcome = handle(db, sender, text, media=fetches)
        outgoing.append((sender, outcome.replies))

    # ONE COMMIT for every message record and everything the bot did. A reply
    # promising "added to your basket" must not survive a failed basket write.
    db.commit()

    # Sending happens AFTER the commit, deliberately. If a send fails we have
    # still recorded the message, so a redelivery is correctly recognised as one
    # rather than replaying the conversation.
    for sender, replies in outgoing:
        for reply in replies:
            try:
                # An image cannot carry buttons in the same message, so a reply
                # with both becomes two: the photo, then the choices. Meta has
                # no combined form, and dropping either half would lose the
                # product picture or the way to buy it.
                if reply.link:
                    # A URL as a BUTTON. Opens in WhatsApp's own browser rather
                    # than the device's, needs no template, and is free inside
                    # the 24-hour window.
                    url, label = reply.link
                    send_cta_url(sender, reply.body, url, label=label)
                elif reply.template:
                    # A template, not free-form: this is the ONLY shape whose
                    # links open in Meta's in-app browser rather than being
                    # handed to the device's default one.
                    name, body_params, button_param = reply.template
                    send_template(
                        sender,
                        name,
                        body_params=body_params,
                        button_param=button_param,
                    )
                elif reply.media_url:
                    send_image(sender, reply.media_url, caption=reply.body)
                    if reply.buttons:
                        send_buttons(sender, "What next?", reply.buttons)
                elif reply.buttons:
                    send_buttons(sender, reply.body, reply.buttons)
                elif reply.rows:
                    send_list(sender, reply.body, reply.list_label, reply.rows)
                else:
                    send_text(sender, reply.body)
            except CloudApiError:
                # Swallowed ON PURPOSE, and only here. Raising would return a
                # non-200, Meta would redeliver, and the buyer would have their
                # basket added to twice to fix a problem that was ours.
                continue

    return JSONResponse({"status": "ok"})
