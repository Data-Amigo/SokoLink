"""
The inbound WhatsApp webhook.

This endpoint is PUBLIC and it will, in the next milestone, create products from
what it is told. Two things stand between that and anyone with the URL:

    the signature   proves the request really came from Twilio
    the SID         proves a redelivery is not a second message

Both are tested here, and the signature is tested in both directions — a forged
request refused, and a genuine one accepted — because a check that rejects
everything looks identical to a working one from the outside.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.webhooks import WHATSAPP_WEBHOOK_PATH
from app.config import get_settings
from app.models import WaMessage

TOKEN = "test-auth-token"
SID = "SM0123456789abcdef0123456789abcdef"


def sign(url: str, params: dict[str, str], token: str = TOKEN) -> str:
    """Twilio's scheme: URL + each key/value in KEY-SORTED order, HMAC-SHA1."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def post(
    client: TestClient, params: dict[str, str], *, signature: str | None = None
) -> httpx.Response:
    """Deliver a webhook, correctly signed unless a test says otherwise."""
    url = f"{get_settings().app_base_url}{WHATSAPP_WEBHOOK_PATH}"
    return client.post(
        WHATSAPP_WEBHOOK_PATH,
        data=params,
        headers={"X-Twilio-Signature": signature or sign(url, params)},
    )


def message(**overrides: str) -> dict[str, str]:
    """A plausible inbound message."""
    params = {
        "MessageSid": SID,
        "From": "whatsapp:+254712345678",
        "To": "whatsapp:+14155238886",
        "Body": "Mixed Ladies Sandals 3000",
        "NumMedia": "0",
    }
    params.update(overrides)
    return params


class TestTheSignature:
    def test_a_genuine_request_is_accepted(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The positive case, so the refusals below prove the check rather than
        a broken endpoint."""
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)

        assert post(client, message()).status_code == 200

    def test_an_unsigned_request_is_refused(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)

        response = client.post(WHATSAPP_WEBHOOK_PATH, data=message())

        assert response.status_code == 403

    def test_a_forged_signature_is_refused(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        THE ONE THAT MATTERS. Without it, anyone with the URL could claim to be
        any seller sending any product.
        """
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)

        response = post(client, message(), signature="not-a-real-signature")

        assert response.status_code == 403
        assert db.scalar(select(WaMessage)) is None

    def test_a_tampered_body_is_refused(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The signature covers the FIELDS, not just the URL — changing the body
        after signing must invalidate it."""
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)
        url = f"{get_settings().app_base_url}{WHATSAPP_WEBHOOK_PATH}"
        good = message()
        signature = sign(url, good)

        tampered = message(Body="Something else entirely")
        response = post(client, tampered, signature=signature)

        assert response.status_code == 403

    def test_it_refuses_to_run_unconfigured(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        With no auth token there is nothing to verify against. Accepting
        unverifiable traffic on a public endpoint that will later create
        products is worse than being unavailable.
        """
        monkeypatch.setattr(get_settings(), "twilio_auth_token", None)

        assert post(client, message()).status_code == 503


class TestIdempotency:
    def test_a_message_is_recorded(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)

        post(client, message())

        record = db.scalar(select(WaMessage).where(WaMessage.provider_message_id == SID))
        assert record is not None
        # Stored bare, so it matches Account.phone without every query
        # remembering to strip the channel prefix.
        assert record.from_number == "254712345678"
        assert record.body == "Mixed Ladies Sandals 3000"

    def test_a_redelivery_changes_nothing(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Twilio retries anything slow. Once this endpoint creates products, a
        second delivery without this guard is a seller's catalogue quietly
        doubling itself.
        """
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)

        for _ in range(4):
            assert post(client, message()).status_code == 200

        rows = db.scalars(select(WaMessage).where(WaMessage.provider_message_id == SID)).all()
        assert len(rows) == 1

    def test_the_raw_payload_is_kept(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a forwarded catalogue parses wrongly, this is the only evidence
        of what actually arrived."""
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)

        post(client, message())

        record = db.scalar(select(WaMessage).where(WaMessage.provider_message_id == SID))
        assert record is not None
        assert record.raw is not None
        assert record.raw["From"] == "whatsapp:+254712345678"

    def test_attachments_are_counted(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A forwarded catalogue is often several images in one message, which
        is why this is a count and not a boolean."""
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)

        post(client, message(NumMedia="3"))

        record = db.scalar(select(WaMessage).where(WaMessage.provider_message_id == SID))
        assert record is not None
        assert record.media_count == 3

    def test_a_message_with_no_sid_is_acknowledged_not_stored(
        self, client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to deduplicate on. Acknowledging beats inviting a retry that
        would arrive equally unidentifiable."""
        monkeypatch.setattr(get_settings(), "twilio_auth_token", TOKEN)
        params = message()
        del params["MessageSid"]

        assert post(client, params).status_code == 200
        assert db.scalar(select(WaMessage)) is None
