"""
Signing in with a WhatsApp number and a one-time code.

**No test sends a WhatsApp message.** ``FakeMessenger`` satisfies the
``Messenger`` Protocol and captures what would have been sent, which is also
how a test learns the code — it is never returned, logged or rendered.

Six digits is one-in-a-million per guess, which is nothing on its own. The
security is entirely in the rails around it, and each one is tested here:

    the attempt cap        a code dies after five wrong guesses
    the expiry            it dies on its own within ten minutes
    the resend cooldown   a number cannot be flooded
    the SIGNED cookie     a browser cannot promote itself from "code sent"
                          to "code proven" — which would be the whole bypass
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models import Account, LoginCode
from app.security import PHONE_COOKIE, create_phone_token
from app.services.messaging import MessagingError, get_messenger
from app.services.otp import MAX_ATTEMPTS, OtpError, request_code, verify_code

PHONE = "254712345678"


class FakeMessenger:
    """Satisfies Messenger. Captures messages; nothing leaves the process."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, body: str) -> str:
        if self.fail:
            raise MessagingError(self.fail)
        self.sent.append((to, body))
        return "SM-fake-id"

    @property
    def last_code(self) -> str:
        """The six digits from the most recent message."""
        import re

        match = re.search(r"\b(\d{6})\b", self.sent[-1][1])
        assert match, f"no code in {self.sent[-1][1]!r}"
        return match.group(1)


@pytest.fixture
def messenger() -> Any:
    """Install the fake for the duration of a test."""
    fake = FakeMessenger()
    app.dependency_overrides[get_messenger] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_messenger, None)


class TestTheCodeItself:
    def test_the_code_is_never_stored_in_the_clear(self, db: Session) -> None:
        """
        For the minutes it lives it is a credential. A database dump, a backup
        or a log containing usable login codes is the same class of mistake as
        plaintext passwords, with a shorter blast radius.
        """
        fake = FakeMessenger()
        request_code(db, PHONE, fake)

        record = db.scalar(select(LoginCode).where(LoginCode.phone == PHONE))
        assert record is not None
        assert fake.last_code not in record.code_hash
        assert record.code_hash.startswith("$argon2")

    def test_request_code_returns_nothing(self, db: Session) -> None:
        """Handing the code back is how it reaches a log line or a template."""
        assert request_code(db, PHONE, FakeMessenger()) is None

    def test_the_right_code_verifies_once(self, db: Session) -> None:
        fake = FakeMessenger()
        request_code(db, PHONE, fake)
        code = fake.last_code

        assert verify_code(db, PHONE, code) is True

        # Spent. A replay must not work.
        with pytest.raises(OtpError):
            verify_code(db, PHONE, code)

    def test_a_wrong_code_is_refused_without_raising(self, db: Session) -> None:
        """False, not an exception — the caller says the same thing either way."""
        fake = FakeMessenger()
        request_code(db, PHONE, fake)

        assert verify_code(db, PHONE, "000000") is False

    def test_the_attempt_cap_kills_the_code(self, db: Session) -> None:
        """THE RAIL THAT MAKES SIX DIGITS SAFE. Unlimited guesses beat it in a
        million tries; five do not."""
        fake = FakeMessenger()
        request_code(db, PHONE, fake)
        code = fake.last_code

        for _ in range(MAX_ATTEMPTS):
            verify_code(db, PHONE, "000000")

        # Even the CORRECT code is now refused.
        with pytest.raises(OtpError, match="Too many attempts"):
            verify_code(db, PHONE, code)

    def test_an_expired_code_is_refused(self, db: Session) -> None:
        fake = FakeMessenger()
        request_code(db, PHONE, fake)
        record = db.scalar(select(LoginCode).where(LoginCode.phone == PHONE))
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.flush()

        with pytest.raises(OtpError, match="expired"):
            verify_code(db, PHONE, fake.last_code)

    def test_a_guess_against_an_expired_code_still_costs_an_attempt(self, db: Session) -> None:
        """
        Counting only live codes would let an attacker guess freely against an
        expired one and learn that nothing is being counted.
        """
        fake = FakeMessenger()
        request_code(db, PHONE, fake)
        record = db.scalar(select(LoginCode).where(LoginCode.phone == PHONE))
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.flush()
        before = record.attempts

        with pytest.raises(OtpError):
            verify_code(db, PHONE, "000000")

        assert record.attempts == before + 1

    def test_a_second_code_cannot_be_requested_immediately(self, db: Session) -> None:
        """Protects the seller's phone from being buried, and us from the bill."""
        fake = FakeMessenger()
        request_code(db, PHONE, fake)

        with pytest.raises(OtpError, match="just sent"):
            request_code(db, PHONE, fake)

    def test_a_send_failure_surfaces_the_provider_message(self, db: Session) -> None:
        """ "Could not send" alone cannot be debugged — the commonest causes are
        specific and fixable."""
        with pytest.raises(OtpError, match="not a valid WhatsApp recipient"):
            request_code(db, PHONE, FakeMessenger(fail="not a valid WhatsApp recipient"))


class TestTheFlow:
    def test_the_login_page_asks_only_for_a_number(self, client: TestClient, db: Session) -> None:
        body = client.get("/seller/login").text
        assert "Continue with WhatsApp" in body
        assert "password" not in body.lower()

    def test_the_later_steps_are_unreachable_without_a_cookie(
        self, client: TestClient, db: Session
    ) -> None:
        for path in ("/seller/verify", "/seller/shop"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/seller/login"

    def test_a_new_number_becomes_a_shop(
        self, client: TestClient, db: Session, messenger: FakeMessenger
    ) -> None:
        """The whole path: number → code → shop name → signed in."""
        client.post("/seller/login", data={"phone": "0712345678"})
        assert messenger.sent, "no code was sent"

        client.post("/seller/verify", data={"code": messenger.last_code})
        response = client.post(
            "/seller/shop", data={"shop_name": "Zuma Fashion Store"}, follow_redirects=False
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"

        account = db.scalar(select(Account).where(Account.phone == PHONE))
        assert account is not None
        assert account.email is None, "a WhatsApp signup should not need an email"
        assert account.seller is not None
        assert account.seller.whatsapp_number == PHONE

    def test_a_known_number_goes_straight_in(
        self, client: TestClient, db: Session, messenger: FakeMessenger
    ) -> None:
        """No shop-name step for someone who already has one."""
        client.post("/seller/login", data={"phone": "0712345678"})
        client.post("/seller/verify", data={"code": messenger.last_code})
        client.post("/seller/shop", data={"shop_name": "Zuma Fashion Store"})

        client.cookies.clear()
        client.post("/seller/login", data={"phone": "0712345678"})
        response = client.post(
            "/seller/verify", data={"code": messenger.last_code}, follow_redirects=False
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"

    def test_a_wrong_code_does_not_sign_anyone_in(
        self, client: TestClient, db: Session, messenger: FakeMessenger
    ) -> None:
        client.post("/seller/login", data={"phone": "0712345678"})

        response = client.post("/seller/verify", data={"code": "000000"}, follow_redirects=False)

        assert response.status_code == 303
        assert "error=" in response.headers["location"]
        assert client.get("/dashboard", follow_redirects=False).status_code in (302, 303, 307)

    def test_the_number_never_appears_in_a_url(
        self, client: TestClient, db: Session, messenger: FakeMessenger
    ) -> None:
        """
        WAYS_OF_WORKING §5. Links leak through history, referrers and
        forwarding, and this one would carry a seller's phone number.
        """
        response = client.post(
            "/seller/login", data={"phone": "0712345678"}, follow_redirects=False
        )

        assert PHONE not in response.headers["location"]
        assert "0712345678" not in response.headers["location"]


class TestTheCookieCannotBeForged:
    def test_an_unverified_cookie_cannot_reach_the_shop_step(
        self, client: TestClient, db: Session, messenger: FakeMessenger
    ) -> None:
        """
        THE BYPASS THIS PREVENTS. Asking for a code proves nothing. If "a code
        was sent" were enough to reach shop creation, anyone could make an
        account for any number they typed.
        """
        client.post("/seller/login", data={"phone": "0712345678"})

        response = client.get("/seller/shop", follow_redirects=False)
        assert response.headers["location"] == "/seller/login"

        created = client.post(
            "/seller/shop", data={"shop_name": "Stolen Shop"}, follow_redirects=False
        )
        assert created.headers["location"] == "/seller/login"
        assert db.scalar(select(Account).where(Account.phone == PHONE)) is None

    def test_a_tampered_cookie_is_refused(self, client: TestClient, db: Session) -> None:
        """The signature is the only thing standing between a text cookie and
        an authentication bypass."""
        good = create_phone_token(PHONE, verified=True)
        client.cookies.set(PHONE_COOKIE, good[:-6] + "aaaaaa")

        response = client.get("/seller/shop", follow_redirects=False)
        assert response.headers["location"] == "/seller/login"

    def test_a_properly_signed_verified_cookie_works(self, client: TestClient, db: Session) -> None:
        """The positive case, so the test above is proving the signature and
        not merely that the page is broken."""
        client.cookies.set(PHONE_COOKIE, create_phone_token(PHONE, verified=True))

        assert client.get("/seller/shop").status_code == 200
