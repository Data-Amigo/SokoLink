"""
Sending a WhatsApp message, behind our own interface.

    text + number ──▶ Messenger.send() ──▶ CloudMessenger (Meta Cloud API)
                            │
                            └── a fake in tests; nothing leaves the process

WHY AN ADAPTER. Callers depend on ``Messenger`` rather than on a provider, so
what actually sends is one class they never see. The project ran on Twilio
first and moved to Meta's Cloud API without those callers changing — which is
the whole point of the seam, and the reason it stays even now there is a single
provider: a future switch is one new class, not a rewrite.

WHY NO SDK. Sending is a single authenticated POST. An SDK would add a
dependency, a version to track and a layer between us and the wire to save a
few lines. The Cloud call lives in ``services/whatsapp_cloud`` alongside the
rest of the Graph surface, and this module just chooses it.

SENDING NEEDS NO WEBHOOK. This is the fact that makes OTP login possible: a
webhook is only required to RECEIVE. We send the code and the seller types it
back into a web page, so nothing has to reach us to log somebody in.

WHAT THIS MODULE DOES NOT DO. It does not decide what to say, when to retry, or
whether a number is allowed to be messaged. It hands one message to the Cloud
API and reports what happened.
"""

from __future__ import annotations

from typing import Protocol


class MessagingError(RuntimeError):
    """
    A message could not be sent.

    Carries the provider's own text, because "could not send" alone cannot be
    debugged — and the commonest causes are specific and fixable: a closed
    24-hour window, a malformed number, an expired token.
    """


class Messenger(Protocol):
    """The seam. Anything that can deliver a WhatsApp message satisfies this."""

    def send(self, to: str, body: str) -> str:
        """
        Deliver ``body`` to ``to``.

        Args:
            to: A phone number in 2547XXXXXXXX form.
            body: Plain text.

        Returns:
            The provider's message id, for correlating with delivery logs.
        """
        ...


class CloudMessenger:
    """
    Meta's WhatsApp Cloud API, behind the one-method seam.

    THE 24-HOUR WINDOW APPLIES TO EVERYTHING SENT THROUGH HERE. Meta allows
    free-form messages only while a conversation is open, which a person opens
    by messaging us. Outside it, only an approved template is delivered. A
    receipt to a buyer who just paid is comfortably inside; a "you have a sale"
    to a seller who has been quiet for a week is not, and will fail. The caller
    has to be able to survive that, which is why send() reports rather than
    hides it.
    """

    def send(self, to: str, body: str) -> str:
        """
        Deliver plain text through the Cloud API.

        Raises:
            MessagingError: On any provider failure, carrying Meta's own text.
                A closed 24-hour window arrives this way.
        """
        from app.services.whatsapp_cloud import CloudApiError, send_text

        try:
            return send_text(to, body)
        except CloudApiError as exc:
            raise MessagingError(str(exc)) from exc


def get_messenger() -> Messenger:
    """
    The messenger the application uses.

    A function rather than a module-level instance so tests can override it as a
    FastAPI dependency, and so nothing is constructed at import time.

    Returns:
        The Cloud API messenger when it is configured.

    Raises:
        MessagingError: If the Cloud API is not configured. Raised here rather
            than at import so the rest of the app still boots — a missing
            WhatsApp key must not take the storefront down.
    """
    from app.config import get_settings

    settings = get_settings()

    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        return CloudMessenger()

    raise MessagingError(
        "WhatsApp is not configured. Set WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID."
    )
