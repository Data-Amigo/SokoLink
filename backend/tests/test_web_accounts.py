"""
Tests for the connect-an-account screens.

``test_verification.py`` covers the service — codes, expiry, attempts, what
counts as proof. This covers the HTTP shell, where a different set of things
go wrong:

  **Claim ids are sequential integers in a URL.** Without a seller scope, any
  signed-in account could read, check or cancel a stranger's claim by guessing
  a number. Three routes take an id; all three are tested.

  **One route spends money on a button press.** ``check_claim`` performs a paid
  Apify scrape. It must be POST-only, and both money guards — the attempt cap
  and the cooldown — must fire BEFORE the scrape, not after.

  **"Not found yet" is not an error.** A seller who has not pressed Save in
  TikTok is the normal case, and rendering that as a failure makes a working
  product feel broken.

The scraper is faked throughout. No test hits Apify.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import AccountClaim, SocialAccount
from app.models.account_claim import MAX_ATTEMPTS
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor
from app.services.accounts import create_account
from app.services.scraper import ScraperError, get_scraper
from app.services.verification import CODE_PREFIX, start_claim
from tests.factories import make_seller

HANDLE = "zumamitumbabales"


class FakeScraper:
    """Returns a profile whose bio may or may not carry the code."""

    def __init__(self, *, bio: str = "", fail: str | None = None) -> None:
        self.bio = bio
        self.fail = fail
        self.calls = 0

    def fetch_profile(self, handle: str, limit: int = 30) -> ScrapedProfile:
        self.calls += 1
        if self.fail:
            raise ScraperError(self.fail)
        return ScrapedProfile(
            author=TikTokAuthor.model_validate(
                {
                    "name": handle,
                    "nickName": "ZUMA MITUMBA BALES",
                    "signature": self.bio,
                    "fans": 22000,
                    "video": 1453,
                }
            ),
            videos=[],
        )

    def fetch_video(self, url: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def download_media(self, url: str, expect: str) -> bytes:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def scraper() -> Generator[FakeScraper, None, None]:
    """
    A fake scraper wired in for the duration of one test.

    Overriding the dependency rather than monkeypatching keeps the production
    wiring (``Depends(get_scraper)``) exactly as it ships.
    """
    fake = FakeScraper()
    app.dependency_overrides[get_scraper] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_scraper, None)


def signed_in(client: TestClient, db: Session, email: str = "seller@example.com") -> None:
    """Create an account and start a browser session as it."""
    create_account(db, email=email, password="correct-horse-battery", shop_name="Nairobi Thrift")
    db.flush()
    client.post("/login", data={"email": email, "password": "correct-horse-battery"})


def claim_for(db: Session, client: TestClient) -> AccountClaim:
    """A pending claim belonging to the signed-in seller."""
    from sqlalchemy import select

    from app.models import Account

    account = db.scalars(select(Account)).first()
    assert account is not None and account.seller is not None
    claim = start_claim(db, account.seller.id, "tiktok", HANDLE)
    db.flush()
    return claim


class TestConnectFlow:
    def test_the_connect_page_loads(self, client: TestClient, db: Session) -> None:
        signed_in(client, db)

        response = client.get("/accounts/connect")

        assert response.status_code == 200
        assert "No password needed" in response.text

    def test_submitting_a_handle_mints_a_code_without_spending_anything(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        """No scrape happens until the seller says they have added the code."""
        signed_in(client, db)

        response = client.post(
            "/accounts/connect", data={"handle": "@" + HANDLE}, follow_redirects=False
        )

        assert response.status_code == 303
        assert scraper.calls == 0, "minting a code must not cost a paid call"
        assert db.query(AccountClaim).count() == 1

    def test_the_code_page_shows_the_code(self, client: TestClient, db: Session) -> None:
        signed_in(client, db)
        claim = claim_for(db, client)

        response = client.get(f"/accounts/claim/{claim.id}")

        assert response.status_code == 200
        assert claim.code in response.text
        assert claim.code.startswith(CODE_PREFIX)

    def test_a_matching_bio_connects_the_account(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        signed_in(client, db)
        claim = claim_for(db, client)
        scraper.bio = f"Nairobi thrift · 0712345678 · {claim.code}"

        response = client.post(f"/accounts/claim/{claim.id}/check", follow_redirects=False)

        assert response.status_code == 303
        assert db.query(SocialAccount).count() == 1
        assert db.query(AccountClaim).count() == 0, "a proven claim is consumed"

    def test_a_missing_code_is_not_reported_as_an_error(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        """
        The seller has probably not pressed Save yet. That is the normal case,
        and calling it a failure makes a working product feel broken.
        """
        signed_in(client, db)
        claim = claim_for(db, client)
        scraper.bio = "just a normal bio"

        response = client.post(f"/accounts/claim/{claim.id}/check")

        assert response.status_code == 200
        assert "Not there yet" in response.text
        assert db.query(SocialAccount).count() == 0


class TestOwnership:
    """Claim ids are guessable integers. Every id-taking route is scoped."""

    def other_sellers_claim(self, db: Session) -> AccountClaim:
        stranger = make_seller(db, slug="stranger", display_name="Stranger")
        claim = start_claim(db, stranger.id, "tiktok", "someoneelse")
        db.flush()
        return claim

    def test_another_sellers_claim_cannot_be_viewed(self, client: TestClient, db: Session) -> None:
        signed_in(client, db)
        theirs = self.other_sellers_claim(db)

        assert client.get(f"/accounts/claim/{theirs.id}").status_code == 404

    def test_another_sellers_claim_cannot_be_checked(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        """The dangerous one: checking spends money against someone else's claim."""
        signed_in(client, db)
        theirs = self.other_sellers_claim(db)

        response = client.post(f"/accounts/claim/{theirs.id}/check", follow_redirects=False)

        assert response.status_code == 404
        assert scraper.calls == 0

    def test_another_sellers_claim_cannot_be_cancelled(
        self, client: TestClient, db: Session
    ) -> None:
        signed_in(client, db)
        theirs = self.other_sellers_claim(db)

        assert client.post(f"/accounts/claim/{theirs.id}/cancel").status_code == 404
        assert db.get(AccountClaim, theirs.id) is not None

    def test_a_missing_claim_looks_the_same_as_someone_elses(
        self, client: TestClient, db: Session
    ) -> None:
        """Both 404, so the endpoint cannot be used to count claims."""
        signed_in(client, db)
        theirs = self.other_sellers_claim(db)

        assert client.get("/accounts/claim/999999").status_code == 404
        assert client.get(f"/accounts/claim/{theirs.id}").status_code == 404


class TestMoneyGuards:
    def test_a_second_check_within_the_cooldown_does_not_scrape(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        """
        A seller mid-bio-edit presses Verify repeatedly. Each press is a
        billable call, so the gap is enforced BEFORE the scrape.
        """
        signed_in(client, db)
        claim = claim_for(db, client)
        scraper.bio = "nothing here"

        client.post(f"/accounts/claim/{claim.id}/check")
        response = client.post(f"/accounts/claim/{claim.id}/check")

        assert scraper.calls == 1, "the cooldown must be checked BEFORE the paid call"
        assert response.status_code == 400
        assert "try again in" in response.text.lower()

    def test_the_cooldown_expires(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        signed_in(client, db)
        claim = claim_for(db, client)
        scraper.bio = "nothing here"

        client.post(f"/accounts/claim/{claim.id}/check")
        claim.last_checked_at = datetime.now(UTC) - timedelta(minutes=5)
        db.flush()

        client.post(f"/accounts/claim/{claim.id}/check")
        assert scraper.calls == 2

    def test_an_exhausted_claim_does_not_scrape(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        signed_in(client, db)
        claim = claim_for(db, client)
        claim.attempts = MAX_ATTEMPTS
        db.flush()

        response = client.post(f"/accounts/claim/{claim.id}/check")

        assert scraper.calls == 0
        assert response.status_code == 400
        assert "Start again" in response.text

    def test_an_expired_claim_does_not_scrape(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        signed_in(client, db)
        claim = claim_for(db, client)
        claim.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db.flush()

        response = client.post(f"/accounts/claim/{claim.id}/check")

        assert scraper.calls == 0
        assert response.status_code == 400
        assert "expired" in response.text.lower()

    def test_checking_is_not_reachable_by_get(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        """
        A GET would fire on every refresh, back button and link preview — each
        one a paid scrape.
        """
        signed_in(client, db)
        claim = claim_for(db, client)

        response = client.get(f"/accounts/claim/{claim.id}/check")

        assert response.status_code == 405
        assert scraper.calls == 0

    def test_a_scrape_failure_still_consumes_the_attempt(
        self, client: TestClient, db: Session, scraper: FakeScraper
    ) -> None:
        """Otherwise a failing profile is an unlimited retry loop against Apify."""
        signed_in(client, db)
        claim = claim_for(db, client)
        scraper.fail = "rate limited"

        response = client.post(f"/accounts/claim/{claim.id}/check")

        assert response.status_code == 400
        assert claim.attempts == 1
        assert claim.last_checked_at is not None


class TestAccountsPage:
    def test_an_empty_state_asks_for_the_first_account(
        self, client: TestClient, db: Session
    ) -> None:
        signed_in(client, db)

        response = client.get("/accounts")

        assert response.status_code == 200
        assert "No accounts connected yet" in response.text

    def test_a_pending_claim_is_surfaced_so_it_can_be_resumed(
        self, client: TestClient, db: Session
    ) -> None:
        """
        A seller who got distracted mid-verification asks "where was I?".
        Starting over would mint a new code and invalidate the one already in
        their bio.
        """
        signed_in(client, db)
        claim = claim_for(db, client)

        response = client.get("/accounts")

        assert claim.code in response.text
        assert f"/accounts/claim/{claim.id}" in response.text

    def test_the_page_is_behind_the_login_wall(self, client: TestClient) -> None:
        response = client.get("/accounts", follow_redirects=False)

        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=%2Faccounts"
