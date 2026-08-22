"""
M-Pesa STK Push, behind our own interface.

    Order + PaymentMethod ──▶ StkEngine.push() ──▶ StkPushResult
                                    │
                                    └── DarajaEngine today; a fake in tests

WHY AN ADAPTER. Daraja is a third party that rate-limits, changes shape, and is
sometimes simply down. Callers depend on ``StkEngine``, never on Daraja, so
tests get a fake and no test ever touches Safaricom. The same seam is already
proven by ``ScraperEngine``.

WHOSE CREDENTIALS THESE ARE. **The seller's, not ours.** We are never in the
money path — the push is made against the seller's own shortcode, using the
passkey and consumer secret they gave us, and the money lands in their account.
There is no platform shortcode anywhere in this file, and there must not be.

THIS PATH IS OPTIONAL, AND MOST SELLERS WILL NOT USE IT. Daraja's STK Push and
C2B APIs work with Paybill and Buy Goods shortcodes only. **Pochi la Biashara
cannot receive an STK push at all**, and a large share of Kenyan micro-sellers
run on Pochi because it needs no business registration. For them the manual
confirmation path in ``services/orders.py`` is not a fallback — it is the whole
product. Nothing here may become a prerequisite for selling.

WHAT THIS MODULE DOES NOT DO. It does not decide whether a payment succeeded.
``push()`` returns only "the prompt was accepted for delivery", which is not
money. **The callback is the only payment truth**, it is processed
idempotently, and that lives in ``services/payments.py``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.models import PaymentMethod
from app.secrets_vault import decrypt

#: Daraja is slow under load and a phone may take a moment to be reached. Long
#: enough not to abandon a live push; short enough that a buyer is not staring
#: at a spinner wondering whether to pay again.
REQUEST_TIMEOUT_SECONDS = 30.0

#: Sandbox and production hosts. Chosen by ``Settings.daraja_environment`` so a
#: misconfigured deploy cannot quietly push real money in a test.
_HOSTS = {
    "sandbox": "https://sandbox.safaricom.co.ke",
    "production": "https://api.safaricom.co.ke",
}


class DarajaError(RuntimeError):
    """
    An STK push could not be made.

    Carries the provider's own message: a bare status code cannot be debugged,
    and this text reaches the seller's dashboard where it has to mean something.
    """


@dataclass(frozen=True)
class StkPushResult:
    """
    What Daraja said when we asked it to prompt a handset.

    **This is not a payment.** It means a prompt was accepted for delivery, and
    nothing more — the buyer may still decline it, mistype their PIN, have no
    balance, or never see it. Only the callback establishes money.
    """

    checkout_request_id: str
    merchant_request_id: str
    customer_message: str


class StkEngine(Protocol):
    """The seam. Anything that can prompt a handset satisfies this."""

    def push(
        self,
        method: PaymentMethod,
        *,
        amount_kes: int,
        phone: str,
        reference: str,
        description: str,
        callback_url: str,
    ) -> StkPushResult:
        """Ask the buyer's handset for ``amount_kes``."""
        ...


def normalise_phone(phone: str) -> str:
    """
    Put a Kenyan number into the 2547XXXXXXXX form Daraja demands.

    Buyers type what they know: ``0712 345 678``, ``+254712345678``,
    ``712345678``. Daraja accepts exactly one of those shapes and rejects the
    rest with an error that does not say so. Normalising here rather than at the
    form means one place to be right, and the buyer is never corrected about how
    to write their own phone number.

    Args:
        phone: A Kenyan mobile number in any common form.

    Returns:
        The number as ``254`` followed by nine digits.

    Raises:
        DarajaError: If it cannot be read as a Kenyan mobile number.
    """
    digits = "".join(c for c in phone if c.isdigit())

    if digits.startswith("254"):
        pass
    elif digits.startswith("0"):
        digits = "254" + digits[1:]
    elif len(digits) == 9:
        digits = "254" + digits
    else:
        raise DarajaError(f"{phone!r} is not a Kenyan mobile number.")

    if len(digits) != 12:
        raise DarajaError(f"{phone!r} is not a Kenyan mobile number.")
    return digits


class DarajaEngine:
    """
    The real thing: Safaricom's Daraja API, called with a seller's credentials.

    Stateless per call. The access token is fetched per push rather than cached,
    because a cached token belonging to seller A must never be used for seller
    B's shortcode — and at our volume the extra round trip costs nothing next to
    the cost of getting that wrong.
    """

    def __init__(self, environment: str = "sandbox") -> None:
        if environment not in _HOSTS:
            raise DarajaError(f"Unknown Daraja environment {environment!r}.")
        self.environment = environment
        self.host = _HOSTS[environment]

    def _access_token(self, method: PaymentMethod) -> str:
        """
        Exchange this seller's consumer key and secret for a bearer token.

        Raises:
            DarajaError: On any failure, including a malformed response. A token
                we cannot read is not a token we should push money with.
        """
        if method.consumer_key_enc is None or method.consumer_secret_enc is None:
            raise DarajaError("This shop has no M-Pesa API credentials saved.")

        key = decrypt(method.consumer_key_enc)
        secret = decrypt(method.consumer_secret_enc)

        try:
            response = httpx.get(
                f"{self.host}/oauth/v1/generate?grant_type=client_credentials",
                auth=(key, secret),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            token = response.json().get("access_token")
        except httpx.HTTPError as exc:
            raise DarajaError(f"Could not reach M-Pesa: {exc}") from exc
        except ValueError as exc:
            raise DarajaError("M-Pesa returned a response we could not read.") from exc

        if not token:
            raise DarajaError("M-Pesa did not return an access token.")
        return str(token)

    def push(
        self,
        method: PaymentMethod,
        *,
        amount_kes: int,
        phone: str,
        reference: str,
        description: str,
        callback_url: str,
    ) -> StkPushResult:
        """
        Prompt the buyer's handset to pay this seller.

        Args:
            method: The seller's payment configuration. Must be STK-capable.
            amount_kes: Whole shillings. Daraja rejects decimals.
            phone: The buyer's number, in any common form.
            reference: The order reference, shown on the buyer's prompt.
            description: Short text for the prompt.
            callback_url: Where Daraja posts the result. Must be publicly
                reachable — Safaricom cannot call localhost, which is why a
                tunnel or a deploy is needed before this can be tried for real.

        Returns:
            An :class:`StkPushResult`. **Not a payment** — see its docstring.

        Raises:
            DarajaError: If the seller is not STK-capable, credentials are
                missing, or Daraja refuses the request.
        """
        if not method.can_stk:
            raise DarajaError(
                "This shop cannot receive M-Pesa prompts. "
                "Pochi la Biashara is not supported by Daraja."
            )
        if method.stk_shortcode is None or method.passkey_enc is None:
            raise DarajaError("This shop has no M-Pesa API credentials saved.")
        if amount_kes < 1:
            raise DarajaError("Amount must be at least KES 1.")

        shortcode = method.stk_shortcode
        passkey = decrypt(method.passkey_enc)

        # Daraja's timestamp and password format, exactly: the password is
        # base64(shortcode + passkey + timestamp) and the timestamp must be the
        # same one sent in the body, or the request is rejected as unauthorised.
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()

        payload: dict[str, Any] = {
            "BusinessShortCode": shortcode,
            "Password": password,
            "Timestamp": timestamp,
            # CustomerBuyGoodsOnline for a till, CustomerPayBillOnline for a
            # paybill. Sending the wrong one is accepted and then fails at the
            # handset, which is the worst place to discover a config error.
            "TransactionType": (
                "CustomerBuyGoodsOnline"
                if method.kind == "till"
                else "CustomerPayBillOnline"
            ),
            "Amount": amount_kes,
            "PartyA": normalise_phone(phone),
            "PartyB": shortcode,
            "PhoneNumber": normalise_phone(phone),
            "CallBackURL": callback_url,
            "AccountReference": method.account_reference or reference,
            "TransactionDesc": description[:100],
        }

        token = self._access_token(method)

        try:
            response = httpx.post(
                f"{self.host}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            body = response.json()
        except httpx.HTTPError as exc:
            raise DarajaError(f"Could not reach M-Pesa: {exc}") from exc
        except ValueError as exc:
            raise DarajaError("M-Pesa returned a response we could not read.") from exc

        # Daraja signals refusal in the BODY, often with HTTP 200. Checking the
        # status code alone would treat "wrong shortcode" as a successful push
        # and leave a buyer waiting for a prompt that will never arrive.
        if str(body.get("ResponseCode", "")) != "0":
            message = body.get("errorMessage") or body.get("ResponseDescription") or str(body)
            raise DarajaError(f"M-Pesa refused the request: {message}")

        return StkPushResult(
            checkout_request_id=str(body["CheckoutRequestID"]),
            merchant_request_id=str(body.get("MerchantRequestID", "")),
            customer_message=str(body.get("CustomerMessage", "")),
        )


def get_stk_engine() -> StkEngine:
    """
    The engine the application uses.

    A function rather than a module-level instance so tests can override it as a
    FastAPI dependency, and so nothing is constructed at import time.
    """
    from app.config import get_settings

    return DarajaEngine(environment=get_settings().daraja_environment)
