"""
Tests for the browser-facing auth layer: pages, cookies and redirects.

``test_auth.py`` covers the service — hashing, timing, password rules. This
covers the HTTP shell around it, where a different set of things go wrong:

  **Anti-enumeration must survive the round trip.** The service refuses to say
  whether an email exists; a template that renders a friendlier message, or a
  status code that differs by cause, gives it straight back.

  **Redirect targets are attacker-controlled.** ``?next=`` arrives from the
  query string. Reflecting it unchecked turns our login page into a phishing
  hop — a genuine biasharamall.com URL that lands on someone else's clone.

  **Cookie flags are invisible until they are wrong.** Nothing fails loudly
  when HttpOnly is dropped; the session simply becomes stealable by any script.

No external service is touched. These are pages and a database.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.security import SESSION_COOKIE
from app.services.accounts import create_account

EMAIL = "seller@example.com"
PASSWORD = "correct-horse-battery"


def register(db: Session, *, email: str = EMAIL, password: str = PASSWORD) -> None:
    """An existing account to sign in as."""
    create_account(db, email=email, password=password, shop_name="Nairobi Thrift")
    db.flush()


def sign_in(client: TestClient, *, email: str = EMAIL, password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


class TestPagesRender:
    def test_the_login_page_loads_for_a_stranger(self, client: TestClient) -> None:
        response = client.get("/login")

        assert response.status_code == 200
        assert "Welcome back" in response.text

    def test_the_signup_page_loads(self, client: TestClient) -> None:
        response = client.get("/signup")

        assert response.status_code == 200
        assert "Create your workspace" in response.text

    def test_the_password_rule_is_stated_before_it_is_hit(self, client: TestClient) -> None:
        """The minimum comes from the constant, so the page cannot drift from it."""
        from app.security import MIN_PASSWORD_LENGTH

        response = client.get("/signup")

        assert f"{MIN_PASSWORD_LENGTH} characters" in response.text


class TestSignup:
    def test_signing_up_lands_on_the_dashboard_already_signed_in(
        self, client: TestClient, db: Session
    ) -> None:
        response = client.post(
            "/signup",
            data={"email": "new@example.com", "password": PASSWORD, "shop_name": "Zuma Bales"},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        assert SESSION_COOKIE in response.cookies

    def test_a_duplicate_email_is_refused_without_losing_the_form(
        self, client: TestClient, db: Session
    ) -> None:
        """Retyping everything after one mistake is how you lose a signup."""
        register(db)

        response = client.post(
            "/signup",
            data={"email": EMAIL, "password": PASSWORD, "shop_name": "Another Shop"},
            follow_redirects=False,
        )

        assert response.status_code == 400
        assert "already exists" in response.text
        assert EMAIL in response.text, "the typed email must survive the failure"
        assert "Another Shop" in response.text

    def test_a_weak_password_is_refused(self, client: TestClient, db: Session) -> None:
        response = client.post(
            "/signup",
            data={"email": "weak@example.com", "password": "short", "shop_name": "Shop"},
            follow_redirects=False,
        )

        assert response.status_code == 400
        assert SESSION_COOKIE not in response.cookies

    def test_the_password_is_never_echoed_back(self, client: TestClient, db: Session) -> None:
        """
        A re-rendered password ends up in proxy logs, browser caches and the
        back button. The email comes back; the password does not.
        """
        register(db)

        response = client.post(
            "/signup",
            data={"email": EMAIL, "password": PASSWORD, "shop_name": "Another Shop"},
            follow_redirects=False,
        )

        assert PASSWORD not in response.text


class TestLogin:
    def test_correct_credentials_start_a_session(self, client: TestClient, db: Session) -> None:
        register(db)

        response = sign_in(client)

        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        assert SESSION_COOKIE in response.cookies

    def test_a_wrong_password_is_refused(self, client: TestClient, db: Session) -> None:
        register(db)

        response = sign_in(client, password="wrong-password-entirely")

        assert response.status_code == 401
        assert SESSION_COOKIE not in response.cookies
        assert EMAIL in response.text, "the typed email must survive the failure"

    def test_an_unknown_email_and_a_wrong_password_are_indistinguishable(
        self, client: TestClient, db: Session
    ) -> None:
        """
        THE anti-enumeration test at the HTTP layer. Same status, same message.
        A stranger must not be able to learn which addresses are registered by
        reading either one.
        """
        register(db)

        wrong_password = sign_in(client, password="wrong-password-entirely")
        unknown_email = sign_in(client, email="nobody@example.com")

        assert wrong_password.status_code == unknown_email.status_code == 401
        assert "Incorrect email or password." in wrong_password.text
        assert "Incorrect email or password." in unknown_email.text

    def test_the_session_cookie_cannot_be_read_by_script(
        self, client: TestClient, db: Session
    ) -> None:
        """HttpOnly is what stops an XSS anywhere on the site becoming account theft."""
        register(db)

        header = sign_in(client).headers["set-cookie"].lower()

        assert "httponly" in header
        assert "samesite=lax" in header


class TestLoginWall:
    def test_an_anonymous_visitor_is_redirected_rather_than_shown_a_401(
        self, client: TestClient
    ) -> None:
        """This is a browser. `{"detail":"Not authenticated"}` is a dead end."""
        response = client.get("/dashboard", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=%2Fdashboard"

    def test_a_signed_in_creator_sees_the_dashboard(self, client: TestClient, db: Session) -> None:
        register(db)
        sign_in(client)

        response = client.get("/dashboard")

        assert response.status_code == 200
        assert "Nairobi Thrift" in response.text

    def test_a_creator_with_no_connected_account_is_told_what_to_do(
        self, client: TestClient, db: Session
    ) -> None:
        """The empty state is the first thing nearly every signup ever sees."""
        register(db)
        sign_in(client)

        response = client.get("/dashboard")

        assert "Connect your TikTok" in response.text

    def test_signing_out_clears_the_session(self, client: TestClient, db: Session) -> None:
        register(db)
        sign_in(client)

        response = client.post("/logout", follow_redirects=False)

        assert response.status_code == 303
        assert client.get("/dashboard", follow_redirects=False).status_code == 303

    def test_an_already_signed_in_creator_is_not_shown_the_login_page(
        self, client: TestClient, db: Session
    ) -> None:
        register(db)
        sign_in(client)

        response = client.get("/login", follow_redirects=False)

        assert response.status_code == 303


class TestRedirectSafety:
    """``?next=`` is attacker-controlled. Every one of these is an open redirect."""

    def test_a_same_site_path_is_honoured(self, client: TestClient, db: Session) -> None:
        register(db)

        response = client.post(
            "/login",
            data={"email": EMAIL, "password": PASSWORD, "next": "/analytics"},
            follow_redirects=False,
        )

        assert response.headers["location"] == "/analytics"

    def test_an_absolute_url_is_discarded(self, client: TestClient, db: Session) -> None:
        register(db)

        response = client.post(
            "/login",
            data={"email": EMAIL, "password": PASSWORD, "next": "https://evil.example/login"},
            follow_redirects=False,
        )

        assert response.headers["location"] == "/dashboard"

    def test_a_protocol_relative_url_is_discarded(self, client: TestClient, db: Session) -> None:
        """``//evil.example`` is a URL, not a path. The easiest one to miss."""
        register(db)

        response = client.post(
            "/login",
            data={"email": EMAIL, "password": PASSWORD, "next": "//evil.example"},
            follow_redirects=False,
        )

        assert response.headers["location"] == "/dashboard"

    def test_a_hostile_next_never_reaches_the_form(self, client: TestClient) -> None:
        """
        Sanitised on the way in as well as on the way out — otherwise the hidden
        field carries the hostile value back to us on the next submit.
        """
        response = client.get("/login?next=https://evil.example", follow_redirects=False)

        assert "evil.example" not in response.text
