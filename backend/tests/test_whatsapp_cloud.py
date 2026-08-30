"""
Meta's WhatsApp Cloud API webhook.

This is the path that can actually launch. The Twilio sandbox requires every
recipient to send ``join <code>`` first, which no real buyer will ever do.

THREE THINGS HERE ARE EASY TO GET WRONG AND SILENT WHEN WRONG:

    the GET handshake   without it the callback URL cannot be REGISTERED, and
                        the dashboard reports only a 404 with no explanation
    the raw body        the signature is over the exact bytes received;
                        re-serialising the parsed JSON changes key order and
                        every genuine message is then rejected as forged
    statuses            delivered/read/failed arrive through the same webhook
                        and outnumber real messages; treating one as a message
                        answers "sent" with a shop menu

Each has a test below, because each looks like working code until it meets real
traffic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.whatsapp_cloud import META_WEBHOOK_PATH
from app.config import get_settings
from app.models import WaMessage
from app.services import whatsapp_cloud

VERIFY_TOKEN = "a-verify-token"
APP_SECRET = "an-app-secret"
WAMID = "wamid.HBgMMjU0NzEyMzQ1Njc4FQIAEhgU"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both secrets present, so a test failing means the logic failed."""
    settings = get_settings()
    monkeypatch.setattr(settings, "whatsapp_verify_token", VERIFY_TOKEN)
    monkeypatch.setattr(settings, "whatsapp_app_secret", APP_SECRET)


@pytest.fixture(autouse=True)
def _no_sending(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Capture replies instead of calling Meta. Tests never hit a paid API."""
    import app.api.whatsapp_cloud as route_module

    sent: list[tuple[str, str]] = []

    def fake_text(to: str, body: str) -> str:
        sent.append((to, body))
        return "wamid.out"

    def fake_image(to: str, url: str, caption: str | None = None) -> str:
        sent.append((to, caption or ""))
        return "wamid.out"

    def fake_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        sent.append((to, body))
        return "wamid.out"

    def fake_list(
        to: str, body: str, label: str, rows: list[tuple[str, str, str]], header: str = ""
    ) -> str:
        sent.append((to, body))
        return "wamid.out"

    def fake_template(
        to: str,
        name: str,
        *,
        language: str = "en",
        body_params: list[str] | None = None,
        button_param: str | None = None,
    ) -> str:
        sent.append((to, f"template:{name}"))
        return "wamid.out"

    # Patched on the ROUTE module, not the service: the route imported these
    # names at import time, so rebinding them in the service would not be seen.
    # EVERY sender, not just the two. A reply carrying buttons would otherwise
    # reach the real function, fail on missing credentials, and be swallowed by
    # the route's deliberate catch — leaving a test that says "nothing was
    # sent" and means "you patched the wrong thing".
    monkeypatch.setattr(route_module, "send_text", fake_text)
    monkeypatch.setattr(route_module, "send_image", fake_image)
    monkeypatch.setattr(route_module, "send_buttons", fake_buttons)
    monkeypatch.setattr(route_module, "send_list", fake_list)
    monkeypatch.setattr(route_module, "send_template", fake_template)
    return sent


def text_payload(
    body: str = "hello", wamid: str = WAMID, sender: str = "254712345678"
) -> dict[str, Any]:
    """A realistic inbound text message, in Meta's four-level envelope."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "102290129340398",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "254700000000",
                                "phone_number_id": "106540352242922",
                            },
                            "contacts": [{"profile": {"name": "Akinyi"}, "wa_id": sender}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": wamid,
                                    "timestamp": "1750000000",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def post(client: TestClient, payload: dict[str, Any], *, signature: str | None = None) -> Any:
    """Deliver a webhook, correctly signed unless a test says otherwise."""
    raw = json.dumps(payload).encode()
    if signature is None:
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"
    return client.post(
        META_WEBHOOK_PATH,
        content=raw,
        headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
    )


class TestTheVerificationHandshake:
    """
    Meta will not save a callback URL until it has GET it and been echoed the
    challenge back. A Twilio-shaped webhook has no GET at all, so the URL can
    never be registered — which is exactly how this project first hit it.
    """

    def test_a_correct_token_echoes_the_challenge(self, client: TestClient) -> None:
        response = client.get(
            META_WEBHOOK_PATH,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "12345",
            },
        )

        assert response.status_code == 200
        assert response.text == "12345"

    def test_the_challenge_is_plain_text_not_json(self, client: TestClient) -> None:
        """
        Meta compares the body byte for byte. A quoted JSON string looks right
        in a browser and fails the comparison.
        """
        response = client.get(
            META_WEBHOOK_PATH,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "12345",
            },
        )

        assert "text/plain" in response.headers["content-type"]
        assert response.text == "12345"
        assert response.text != '"12345"'

    def test_a_wrong_token_is_refused_and_echoes_nothing(self, client: TestClient) -> None:
        """Echoing the challenge to anyone who asks would let a stranger
        register our URL against their own app."""
        response = client.get(
            META_WEBHOOK_PATH,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "12345",
            },
        )

        assert response.status_code == 403
        assert "12345" not in response.text

    def test_a_mode_other_than_subscribe_is_refused(self, client: TestClient) -> None:
        response = client.get(
            META_WEBHOOK_PATH,
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "12345",
            },
        )

        assert response.status_code == 403

    def test_it_refuses_to_run_unconfigured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "whatsapp_verify_token", None)

        response = client.get(
            META_WEBHOOK_PATH,
            params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"},
        )

        assert response.status_code == 503


class TestTheSignature:
    def test_a_genuine_request_is_accepted(self, client: TestClient, db: Session) -> None:
        assert post(client, text_payload()).status_code == 200

    def test_an_unsigned_request_is_refused(self, client: TestClient, db: Session) -> None:
        response = client.post(META_WEBHOOK_PATH, json=text_payload())

        assert response.status_code == 403

    def test_a_forged_signature_is_refused(self, client: TestClient, db: Session) -> None:
        """
        THE ONE THAT MATTERS. This endpoint creates products from what it is
        told, so a forged "seller X forwarded this" must never be believed.
        """
        response = post(client, text_payload(), signature="sha256=deadbeef")

        assert response.status_code == 403
        assert db.scalar(select(WaMessage)) is None

    def test_a_tampered_body_is_refused(self, client: TestClient, db: Session) -> None:
        """The digest covers the body, so changing it after signing must fail."""
        raw = json.dumps(text_payload()).encode()
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()

        response = client.post(
            META_WEBHOOK_PATH,
            content=json.dumps(text_payload(body="something else")).encode(),
            headers={"X-Hub-Signature-256": f"sha256={digest}"},
        )

        assert response.status_code == 403

    def test_it_refuses_to_run_without_an_app_secret(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "whatsapp_app_secret", None)

        assert post(client, text_payload()).status_code == 503


class TestReadingTheEnvelope:
    def test_a_text_message_is_recorded(self, client: TestClient, db: Session) -> None:
        post(client, text_payload(body="Mixed Ladies Sandals"))

        record = db.scalar(select(WaMessage).where(WaMessage.provider_message_id == WAMID))
        assert record is not None
        assert record.from_number == "254712345678"
        assert record.body == "Mixed Ladies Sandals"

    def test_a_redelivery_changes_nothing(self, client: TestClient, db: Session) -> None:
        """Meta retries anything slow and disables webhooks that keep failing,
        so a second delivery must be a quiet 200."""
        for _ in range(4):
            assert post(client, text_payload()).status_code == 200

        rows = db.scalars(select(WaMessage).where(WaMessage.provider_message_id == WAMID)).all()
        assert len(rows) == 1

    def test_a_status_callback_is_not_a_message(self, client: TestClient, db: Session) -> None:
        """
        Delivered/read/failed arrive through this same webhook and outnumber
        real messages several to one. Treating one as a message would answer
        "sent" with a shop menu.
        """
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "statuses": [
                                    {"id": WAMID, "status": "delivered", "recipient_id": "254712"}
                                ],
                            },
                        }
                    ],
                }
            ],
        }

        assert post(client, payload).status_code == 200
        assert db.scalar(select(WaMessage)) is None

    def test_an_image_caption_becomes_the_text(self, client: TestClient, db: Session) -> None:
        """
        Meta puts the caption INSIDE the image object. A seller forwarding a
        photo captioned "Ankara shirts 1800" would otherwise have the price
        thrown away before the model ever saw it.
        """
        payload = text_payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
            "from": "254712345678",
            "id": WAMID,
            "type": "image",
            "image": {"id": "media-123", "mime_type": "image/jpeg", "caption": "Ankara 1800"},
        }

        post(client, payload)

        record = db.scalar(select(WaMessage).where(WaMessage.provider_message_id == WAMID))
        assert record is not None
        assert record.body == "Ankara 1800"
        assert record.media_count == 1

    def test_an_unknown_envelope_is_acknowledged(self, client: TestClient, db: Session) -> None:
        """Meta adds fields. An unrecognised shape must be a quiet 200, not a
        500 that gets our webhook disabled."""
        assert post(client, {"object": "whatsapp_business_account"}).status_code == 200
        assert post(client, {}).status_code == 200

    def test_an_unreadable_body_is_acknowledged(self, client: TestClient, db: Session) -> None:
        raw = b"{not json at all"
        digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()

        response = client.post(
            META_WEBHOOK_PATH, content=raw, headers={"X-Hub-Signature-256": f"sha256={digest}"}
        )

        assert response.status_code == 200


class TestReplying:
    def test_the_bot_reply_is_sent_not_returned(
        self, client: TestClient, db: Session, _no_sending: list[tuple[str, str]]
    ) -> None:
        """
        Meta has no TwiML. Every reply is a separate authenticated call, so a
        webhook that only returns a body says nothing to anybody.
        """
        post(client, text_payload(body="hi"))

        assert _no_sending, "nothing was sent"
        to, body = _no_sending[0]
        assert to == "254712345678"
        assert "Biashara Mall" in body

    def test_a_failed_send_does_not_fail_the_webhook(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        THE MESSAGE IS ALREADY RECORDED when sending happens. A non-200 here
        would have Meta redeliver and replay the whole conversation step — so a
        buyer would have their basket added to twice to fix a problem of ours.
        """
        import app.api.whatsapp_cloud as route_module

        def boom(*_: object, **__: object) -> str:
            raise whatsapp_cloud.CloudApiError("token expired")

        monkeypatch.setattr(route_module, "send_text", boom)

        response = post(client, text_payload())

        assert response.status_code == 200
        assert db.scalar(select(WaMessage)) is not None


class TestTheClientItself:
    def test_extract_messages_ignores_everything_that_is_not_one(self) -> None:
        assert whatsapp_cloud.extract_messages({}) == []
        assert whatsapp_cloud.extract_messages({"entry": [{"changes": [{"value": {}}]}]}) == []

    def test_an_interactive_reply_is_read_as_its_ID(self) -> None:
        """
        THE ID, NOT THE TITLE. We set both when sending. The id is a stable key
        the conversation matches on; the title is written for a person and gets
        reworded — a rewrite of "Add to basket" must not silently stop adding
        to the basket.
        """
        message = {
            "from": "254712345678",
            "id": WAMID,
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {"id": "cat:Shoes", "title": "Shoes"},
            },
        }

        _, sender, text, media = whatsapp_cloud.read_message(message)

        assert sender == "254712345678"
        assert text == "cat:Shoes"
        assert media == []

    def test_a_reply_with_no_id_falls_back_to_its_title(self) -> None:
        """Not every interactive reply is one of ours — a template button we
        did not define still has to mean something."""
        message = {
            "from": "254712345678",
            "id": WAMID,
            "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"title": "Shoes"}},
        }

        _, _, text, _ = whatsapp_cloud.read_message(message)

        assert text == "Shoes"

    def test_read_message_is_silent_on_types_it_cannot_use(self) -> None:
        """Audio, location, stickers. Acknowledged, never guessed at."""
        _, _, text, media = whatsapp_cloud.read_message(
            {"from": "254712345678", "id": WAMID, "type": "audio", "audio": {"id": "x"}}
        )

        assert text == ""
        assert media == []
