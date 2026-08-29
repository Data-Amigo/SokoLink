"""
Meta's WhatsApp Cloud API — sending, and fetching media.

    reply text   ──▶ POST /{phone_number_id}/messages
    reply image  ──▶ POST /{phone_number_id}/messages  (type=image, link)
    inbound media ──▶ GET /{media_id} ──▶ {"url": …} ──▶ GET url ──▶ bytes

WHY THIS EXISTS ALONGSIDE messaging.py. Twilio was the provider we could use
immediately: no Meta business verification, and the sandbox works in an hour.
That was always a staging post — the sandbox requires every recipient to send
``join <code>`` first, which no real buyer will ever do, so it cannot launch.
This is the path that can.

THE BIGGEST DIFFERENCE IS NOT THE PAYLOAD, IT IS THE REPLY. Twilio lets the
webhook answer in its own HTTP response (TwiML), so a reply costs no extra call
and needs no credentials on that path. Meta has no such thing: the webhook must
return 200 immediately and every reply is a separate authenticated POST. Code
written against TwiML does not port — it has to be reshaped around sending.

MEDIA TAKES TWO ROUND TRIPS, and both need the bearer token. The first returns a
short-lived signed URL, the second returns the bytes. Fetching that URL without
the token yields an error document, which as image bytes would reach the vision
model as garbage and produce a confident draft of nothing.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings

#: Pinned rather than floating. Meta changes response shapes between versions,
#: and "it broke on a Tuesday" is not a debugging story anybody enjoys.
GRAPH_VERSION = "v21.0"
GRAPH_ROOT = f"https://graph.facebook.com/{GRAPH_VERSION}"

#: Long enough to absorb a slow hop to Meta, short enough that a stuck call
#: cannot hold a webhook worker open past Meta's own patience.
REQUEST_TIMEOUT_SECONDS = 20.0


class CloudApiError(RuntimeError):
    """
    A Cloud API call failed.

    Carries Meta's own message. Their errors are specific and actionable — an
    expired token, an unverified number, a 24-hour window that has closed — and
    each needs a different response from us. "Sending failed" tells nobody
    anything.
    """


def _require(name: str, value: str | None) -> str:
    """Fail with the variable name rather than a None deep inside httpx."""
    if not value:
        raise CloudApiError(f"{name} is not set. The WhatsApp Cloud API cannot be used without it.")
    return value


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    One authenticated POST to the Graph API.

    Args:
        path: Path below the version root, no leading slash.
        payload: JSON body.

    Returns:
        The decoded response.

    Raises:
        CloudApiError: On transport failure or any non-2xx, carrying Meta's text.
    """
    token = _require("WHATSAPP_ACCESS_TOKEN", settings.whatsapp_access_token)

    try:
        response = httpx.post(
            f"{GRAPH_ROOT}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise CloudApiError(f"Could not reach the WhatsApp API: {exc}") from exc

    if response.status_code >= 400:
        raise CloudApiError(f"WhatsApp API returned {response.status_code}: {response.text}")

    return dict(response.json())


def send_text(to: str, body: str) -> str:
    """
    Send a plain text message.

    Args:
        to: Recipient, digits with country code and no plus.
        body: The text.

    Returns:
        Meta's message id (``wamid.…``).

    Raises:
        CloudApiError: If the send fails.
    """
    phone_id = _require("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id)

    result = _post(
        f"{phone_id}/messages",
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "text",
            # Link previews off: our messages carry shop links, and a preview
            # card pushes the actual instruction off a phone screen.
            "text": {"preview_url": False, "body": body},
        },
    )
    return str(result.get("messages", [{}])[0].get("id", ""))


def send_image(to: str, image_url: str, caption: str | None = None) -> str:
    """
    Send an image by URL.

    Args:
        to: Recipient, digits with country code.
        image_url: A PUBLICLY reachable https URL. Meta fetches this itself.
        caption: Optional text shown under the image.

    Returns:
        Meta's message id.

    Raises:
        CloudApiError: If the send fails.

    Notes:
        META FETCHES THE URL, our server does not push the bytes. So a link to
        localhost, or to anything behind our login wall, silently produces a
        message with no image — the storefront's product covers must be public.
    """
    phone_id = _require("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id)

    image: dict[str, Any] = {"link": image_url}
    if caption:
        image["caption"] = caption

    result = _post(
        f"{phone_id}/messages",
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "image",
            "image": image,
        },
    )
    return str(result.get("messages", [{}])[0].get("id", ""))


def download_media(media_id: str) -> bytes:
    """
    Fetch one inbound media file, by Meta's media id.

    Args:
        media_id: From the inbound message payload.

    Returns:
        The bytes.

    Raises:
        CloudApiError: On any failure.

    Notes:
        TWO ROUND TRIPS, BOTH AUTHENTICATED. The first call returns a
        short-lived signed URL; the second returns the bytes. The signed URL
        still requires the bearer token — fetching it without one returns an
        error document, and error-document-as-JPEG reaches the vision model as
        garbage it will confidently describe.
    """
    token = _require("WHATSAPP_ACCESS_TOKEN", settings.whatsapp_access_token)
    headers = {"Authorization": f"Bearer {token}"}

    try:
        lookup = httpx.get(
            f"{GRAPH_ROOT}/{media_id}", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        raise CloudApiError(f"Could not look up media {media_id}: {exc}") from exc

    if lookup.status_code >= 400:
        raise CloudApiError(f"Media lookup returned {lookup.status_code}: {lookup.text}")

    url = lookup.json().get("url")
    if not url:
        raise CloudApiError(f"Media {media_id} has no download URL.")

    try:
        content = httpx.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        raise CloudApiError(f"Could not download media {media_id}: {exc}") from exc

    if content.status_code >= 400:
        raise CloudApiError(f"Media download returned {content.status_code}")

    return content.content


def extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Pull the actual messages out of Meta's nested webhook envelope.

    Args:
        payload: The decoded webhook body.

    Returns:
        A flat list of message objects, possibly empty.

    Notes:
        THE ENVELOPE IS FOUR LEVELS DEEP and mostly carries things that are NOT
        messages. ``statuses`` — delivered, read, failed — arrive through the
        same webhook and outnumber real messages several to one. Treating a
        status as a message would answer "sent" with a shop menu.

        Every level is defensive because this payload comes from outside and a
        shape change on Meta's side must not raise; an unrecognised envelope is
        an empty list, which the caller acknowledges and ignores.
    """
    messages: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for message in value.get("messages") or []:
                if isinstance(message, dict):
                    messages.append(message)
    return messages


def read_message(message: dict[str, Any]) -> tuple[str, str, str, list[tuple[str, str]]]:
    """
    Flatten one inbound message into the shape the bot understands.

    Args:
        message: One element from :func:`extract_messages`.

    Returns:
        ``(message_id, from_number, text, media)`` where ``media`` is a list of
        ``(media_id, media_id)`` pairs — the id twice, because Cloud API media
        is fetched BY id rather than by URL, and the caller supplies the fetch.

    Notes:
        AN IMAGE'S CAPTION IS THE TEXT. Meta puts it inside the image object
        rather than in a separate text field, so a seller who forwards a photo
        with "Ankara shirts 1800" would otherwise have their price thrown away.
    """
    message_id = str(message.get("id") or "")
    sender = str(message.get("from") or "").lstrip("+")
    kind = message.get("type")

    if kind == "text":
        return message_id, sender, str((message.get("text") or {}).get("body") or ""), []

    if kind == "image":
        image = message.get("image") or {}
        media_id = str(image.get("id") or "")
        caption = str(image.get("caption") or "")
        return message_id, sender, caption, ([(media_id, media_id)] if media_id else [])

    if kind == "interactive":
        # Button and list replies. THE ID, NOT THE TITLE. We set both; the id is
        # a stable key the conversation matches on, while the title is written
        # for a person and gets reworded — a rewrite of "Add to basket" must not
        # silently stop adding to the basket.
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            chosen = interactive.get(key) or {}
            if chosen.get("id"):
                return message_id, sender, str(chosen["id"]), []
            if chosen.get("title"):
                return message_id, sender, str(chosen["title"]), []
        return message_id, sender, "", []

    if kind == "button":
        return message_id, sender, str((message.get("button") or {}).get("text") or ""), []

    # Audio, video, documents, locations, contacts, stickers, reactions. We
    # acknowledge them and say nothing rather than guessing at an intent.
    return message_id, sender, "", []


def payload_summary(payload: dict[str, Any]) -> str:
    """A compact rendering of the envelope, for storing beside the message."""
    return json.dumps(payload)[:8000]


# ══════════════════════════════════════════════════════════════════════════
# INTERACTIVE MESSAGES
# ══════════════════════════════════════════════════════════════════════════
#
# The reason moving to Meta was worth it for the buyer. Twilio needed a
# pre-approved content template per shape, which is why buyers got numbered
# text menus. Here they can be sent free-form inside the 24-hour window a
# person opens by messaging first — no approval, no template.
#
# EVERY LIMIT BELOW IS ENFORCED BY META, NOT SUGGESTED. Exceed one and the
# whole message is rejected: the buyer sees nothing, and the failure arrives as
# an API error rather than as anything visible in the chat. Truncating here is
# the difference between an ugly label and a silent dead end.

#: Reply buttons per message. Meta's hard limit.
MAX_BUTTONS = 3

#: Rows in a list message, across all sections.
MAX_ROWS = 10

#: Character caps. A title that overflows fails the whole send.
BUTTON_TITLE_CHARS = 20
ROW_TITLE_CHARS = 24
ROW_DESCRIPTION_CHARS = 72
LIST_LABEL_CHARS = 20
BODY_CHARS = 1024


def _clip(text: str, limit: int) -> str:
    """
    Fit text to one of Meta's caps, with an ellipsis when it had to be cut.

    A cut label is worse than a short one, and both are far better than a
    message that is never delivered.
    """
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> str:
    """
    Send up to three reply buttons.

    Args:
        to: Recipient, digits with country code.
        body: The question above the buttons.
        buttons: ``(id, title)`` pairs. The id comes back to us on tap, which
            is why it — not the title — is what the conversation matches on:
            a title is written for a person and may be reworded any time.

    Returns:
        Meta's message id.

    Raises:
        CloudApiError: If the send fails.
    """
    phone_id = _require("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id)

    result = _post(
        f"{phone_id}/messages",
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": _clip(body, BODY_CHARS)},
                "action": {
                    "buttons": [
                        {
                            "type": "reply",
                            "reply": {"id": bid, "title": _clip(title, BUTTON_TITLE_CHARS)},
                        }
                        for bid, title in buttons[:MAX_BUTTONS]
                    ]
                },
            },
        },
    )
    return str(result.get("messages", [{}])[0].get("id", ""))


def send_list(
    to: str, body: str, label: str, rows: list[tuple[str, str, str]], header: str = ""
) -> str:
    """
    Send a list picker — the component that replaces a numbered menu.

    Args:
        to: Recipient, digits with country code.
        body: The text above the button that opens the list.
        label: The button that opens it. Kept short; it is not a sentence.
        rows: ``(id, title, description)``. The description is where a price
            belongs — it is the second line a buyer reads, and putting it in
            the title costs the item name its room.
        header: Optional bold line above the body.

    Returns:
        Meta's message id.

    Raises:
        CloudApiError: If the send fails.

    Notes:
        TEN ROWS, TOTAL. A catalogue longer than that is why categories exist;
        silently sending the first ten of forty would hide stock a seller
        believes is listed.
    """
    phone_id = _require("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id)

    interactive: dict[str, Any] = {
        "type": "list",
        "body": {"text": _clip(body, BODY_CHARS)},
        "action": {
            "button": _clip(label, LIST_LABEL_CHARS),
            "sections": [
                {
                    "rows": [
                        {
                            "id": rid,
                            "title": _clip(title, ROW_TITLE_CHARS),
                            **({"description": _clip(desc, ROW_DESCRIPTION_CHARS)} if desc else {}),
                        }
                        for rid, title, desc in rows[:MAX_ROWS]
                    ]
                }
            ],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": _clip(header, 60)}

    result = _post(
        f"{phone_id}/messages",
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to.lstrip("+"),
            "type": "interactive",
            "interactive": interactive,
        },
    )
    return str(result.get("messages", [{}])[0].get("id", ""))
