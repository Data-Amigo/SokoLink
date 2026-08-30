"""
Sending a WhatsApp message, behind our own interface.

    text + number ──▶ Messenger.send() ──▶ TwilioMessenger today
                            │
                            └── a fake in tests; nothing leaves the process

WHY AN ADAPTER, AND WHY IT MATTERS MORE HERE THAN USUAL. The WhatsApp provider
is genuinely undecided: Twilio works today with no Meta business verification,
and Meta's Cloud API is cheaper at volume once verification lands. Callers
depend on ``Messenger``, so switching is one new class rather than a rewrite.

WHY NO TWILIO SDK. Sending is a single form POST with basic auth. The SDK would
add a dependency, a version to track and a layer between us and the wire, to
save four lines. ``httpx`` is already here for Daraja.

SENDING NEEDS NO WEBHOOK. This is the fact that makes OTP login possible before
the bot exists: a webhook is only required to RECEIVE. We send the code and the
seller types it back into a web page, so nothing has to reach us from Meta.

WHAT THIS MODULE DOES NOT DO. It does not decide what to say, when to retry, or
whether a number is allowed to be messaged. It formats one request and reports
what happened.
"""

from __future__ import annotations

from typing import Protocol

import httpx

#: Twilio is usually fast, but a seller is staring at a spinner waiting for a
#: code. Long enough to absorb a slow hop, short enough to fail visibly.
REQUEST_TIMEOUT_SECONDS = 15.0

_TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"


class MessagingError(RuntimeError):
    """
    A message could not be sent.

    Carries the provider's own text, because "could not send" alone cannot be
    debugged — and the commonest causes are specific and fixable: an unverified
    sandbox recipient, a malformed number, an expired token.
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


class TwilioMessenger:
    """
    Twilio's WhatsApp API, called over plain HTTP.

    THE SANDBOX HAS A TRAP WORTH KNOWING. On Twilio's WhatsApp sandbox every
    recipient must first send ``join <code>`` to the sandbox number, or the
    message is accepted by the API and silently never delivered. That is fine
    for our own testing and impossible for real sellers — production needs an
    approved sender.
    """

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        # Twilio wants the whatsapp: scheme and a leading +. Sellers and our own
        # config store bare 254…, so normalise here rather than asking every
        # caller and every .env to remember the format.
        self.from_number = self._as_whatsapp(from_number)

    @staticmethod
    def _as_whatsapp(number: str) -> str:
        """Put a number into Twilio's ``whatsapp:+254…`` form, however it arrived."""
        cleaned = number.strip().replace("whatsapp:", "").replace(" ", "")
        if not cleaned.startswith("+"):
            cleaned = f"+{cleaned.lstrip('+')}"
        return f"whatsapp:{cleaned}"

    def send(self, to: str, body: str) -> str:
        """
        Send one WhatsApp message.

        Raises:
            MessagingError: On any transport or provider failure. Twilio reports
                refusals in the BODY with a ``message`` field, so the status code
                alone is not enough — a 400 with "not a valid WhatsApp
                recipient" has to reach the caller as that sentence.
        """
        try:
            response = httpx.post(
                _TWILIO_API.format(sid=self.account_sid),
                auth=(self.account_sid, self.auth_token),
                data={
                    "From": self.from_number,
                    "To": self._as_whatsapp(to),
                    "Body": body,
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            payload = response.json()
        except httpx.HTTPError as exc:
            raise MessagingError(f"Could not reach WhatsApp: {exc}") from exc
        except ValueError as exc:
            raise MessagingError("WhatsApp returned a response we could not read.") from exc

        if response.status_code >= 400:
            raise MessagingError(payload.get("message") or f"WhatsApp refused: {payload}")

        return str(payload.get("sid", ""))


class CloudMessenger:
    """
    Meta's WhatsApp Cloud API, behind the same one-method seam.

    THIS IS WHAT THE ADAPTER WAS FOR. The module docstring above predicted the
    switch — "switching is one new class rather than a rewrite" — and this is
    that class. Callers depending on ``Messenger`` did not change.

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
        The Cloud API when it is configured, Twilio otherwise.

    Raises:
        MessagingError: If neither provider is configured. Raised here rather
            than at import so the rest of the app still boots — a missing
            WhatsApp key must not take the storefront down.

    Notes:
        META IS PREFERRED WHEN PRESENT, and the order matters. Both sets of
        credentials will sit side by side during the switch, and a deployment
        that has configured Meta has done so deliberately; silently continuing
        to send through Twilio would mean messages arriving from a number the
        seller has stopped watching.
    """
    from app.config import get_settings

    settings = get_settings()

    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        return CloudMessenger()

    if (
        settings.twilio_account_sid
        and settings.twilio_auth_token
        and settings.twilio_whatsapp_number
    ):
        return TwilioMessenger(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_number=settings.twilio_whatsapp_number,
        )

    raise MessagingError(
        "WhatsApp is not configured. Set WHATSAPP_ACCESS_TOKEN and "
        "WHATSAPP_PHONE_NUMBER_ID, or the three TWILIO_* variables."
    )
