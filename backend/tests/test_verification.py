"""
Tests for social account ownership verification.

The attack this defends against is specific: a stranger types someone else's
handle, we scrape her videos and photos, and they publish a storefront pointing
at THEIR WhatsApp number. Sales diversion, invisible to the buyer.

So the load-bearing test in this file is the one asserting an unverified
account **cannot be synced**. Everything else supports it.

The scraper is faked throughout. No test calls a paid API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.models import Platform, SocialAccount, VerificationMethod
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor
from app.services.scraper import ScraperError
from app.services.verification import (
    CODE_PREFIX,
    VerificationError,
    check_verification,
    generate_code,
    require_syncable,
    start_verification,
    verify_via_oauth,
)
from tests.factories import make_account, make_seller


class FakeScraper:
    """Returns a profile with whatever bio the test wants."""

    def __init__(self, *, bio: str | None = "", fail: str | None = None) -> None:
        self.bio = bio
        self.fail = fail
        self.calls = 0

    def fetch_profile(self, handle: str, limit: int = 30) -> ScrapedProfile:
        self.calls += 1
        if self.fail:
            raise ScraperError(self.fail)
        return ScrapedProfile(
            author=TikTokAuthor.model_validate(
                {"name": handle, "signature": self.bio, "fans": 100, "video": 5}
            ),
            videos=[],
        )

    def fetch_video(self, url: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def download_media(self, url: str, expect: str) -> bytes:  # pragma: no cover
        raise NotImplementedError


class TestCodeGeneration:
    def test_a_code_is_prefixed_and_recognisable(self) -> None:
        assert generate_code().startswith(CODE_PREFIX)

    def test_codes_are_unique(self) -> None:
        assert len({generate_code() for _ in range(200)}) > 190

    def test_codes_avoid_characters_that_are_easy_to_mistype(self) -> None:
        """
        A seller retypes this on a phone keyboard. 0/O and 1/I/l are exactly
        where that goes wrong, so the alphabet excludes them.
        """
        for _ in range(100):
            body = generate_code().removeprefix(CODE_PREFIX)
            assert not (set(body) & set("O0I1L"))


class TestStartingVerification:
    def test_issues_a_code_and_an_expiry(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        code = start_verification(account)

        assert account.verification_code == code
        assert account.verification_expires_at is not None

    def test_asking_again_issues_a_new_code(self, db: Session) -> None:
        """
        A seller asking again usually lost the first one. Reissuing costs
        nothing; reusing would extend a live code's lifetime indefinitely.
        """
        account = make_account(db, make_seller(db))
        first = start_verification(account)
        second = start_verification(account)

        assert first != second
        assert account.verification_code == second

    def test_an_already_verified_account_is_refused(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        account.verified_at = datetime.now(UTC)
        account.verification_method = VerificationMethod.OAUTH.value

        with pytest.raises(VerificationError, match="already verified"):
            start_verification(account)


class TestCheckingVerification:
    def test_a_matching_bio_verifies_the_account(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        code = start_verification(account)

        assert check_verification(account, FakeScraper(bio=f"my shop {code}")) is True
        assert account.is_verified is True
        assert account.verification_method == VerificationMethod.BIO_CODE.value

    def test_the_code_is_cleared_once_used(self, db: Session) -> None:
        """A live code lying around is a standing target, and it has done its job."""
        account = make_account(db, make_seller(db))
        code = start_verification(account)
        check_verification(account, FakeScraper(bio=code))

        assert account.verification_code is None
        assert account.verification_expires_at is None

    def test_matching_ignores_case(self, db: Session) -> None:
        """Phone keyboards auto-capitalise; rejecting that is needless friction."""
        account = make_account(db, make_seller(db))
        code = start_verification(account)

        assert check_verification(account, FakeScraper(bio=code.upper())) is True

    def test_a_code_buried_in_a_real_bio_is_found(self, db: Session) -> None:
        """Real bios are full of phone numbers and shop names — spike 02 confirmed."""
        account = make_account(db, make_seller(db))
        code = start_verification(account)
        bio = f"zuma bales and LADIESWEAr contact us 0105515839/0754234636 {code}"

        assert check_verification(account, FakeScraper(bio=bio)) is True

    def test_a_missing_code_leaves_the_account_unverified(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        start_verification(account)

        assert check_verification(account, FakeScraper(bio="no code here")) is False
        assert account.is_verified is False

    def test_an_empty_bio_leaves_the_account_unverified(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        start_verification(account)

        assert check_verification(account, FakeScraper(bio=None)) is False

    def test_someone_elses_code_does_not_verify(self, db: Session) -> None:
        """The core of it: only THIS account's code counts."""
        account = make_account(db, make_seller(db))
        start_verification(account)

        assert check_verification(account, FakeScraper(bio=generate_code())) is False
        assert account.is_verified is False

    def test_checking_without_a_code_is_refused(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        with pytest.raises(VerificationError, match="No verification in progress"):
            check_verification(account, FakeScraper())

    def test_an_expired_code_is_refused(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        code = start_verification(account)
        account.verification_expires_at = datetime.now(UTC) - timedelta(minutes=1)

        with pytest.raises(VerificationError, match="expired"):
            check_verification(account, FakeScraper(bio=code))

    def test_an_expired_code_is_not_even_checked(self, db: Session) -> None:
        """Expiry short-circuits BEFORE the paid scrape."""
        account = make_account(db, make_seller(db))
        code = start_verification(account)
        account.verification_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        scraper = FakeScraper(bio=code)

        with pytest.raises(VerificationError):
            check_verification(account, scraper)
        assert scraper.calls == 0

    def test_a_scrape_failure_explains_itself(self, db: Session) -> None:
        """A private profile and a rate limit need different responses."""
        account = make_account(db, make_seller(db))
        start_verification(account)

        with pytest.raises(VerificationError, match="private"):
            check_verification(account, FakeScraper(fail="profile may be private"))

    def test_a_naive_expiry_does_not_crash(self, db: Session) -> None:
        """Rows written before the column was timezone-aware come back naive."""
        account = make_account(db, make_seller(db))
        code = start_verification(account)
        account.verification_expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(
            hours=1
        )

        assert check_verification(account, FakeScraper(bio=code)) is True


class TestOAuthVerification:
    def test_a_matching_handle_verifies(self, db: Session) -> None:
        account = make_account(db, make_seller(db), handle="nairobithrift")
        verify_via_oauth(account, "nairobithrift")

        assert account.is_verified is True
        assert account.verification_method == VerificationMethod.OAUTH.value

    def test_the_at_sign_and_case_are_tolerated(self, db: Session) -> None:
        account = make_account(db, make_seller(db), handle="nairobithrift")
        verify_via_oauth(account, "@NairobiThrift")
        assert account.is_verified is True

    def test_signing_in_as_someone_else_is_refused(self, db: Session) -> None:
        """
        The whole value of OAuth: the handle comes from the PROVIDER, never
        from anything the seller typed. A mismatch means they authenticated as
        a different account.
        """
        account = make_account(db, make_seller(db), handle="nairobithrift")

        with pytest.raises(VerificationError, match="signed in as"):
            verify_via_oauth(account, "someoneelse")
        assert account.is_verified is False

    def test_oauth_clears_any_outstanding_bio_code(self, db: Session) -> None:
        account = make_account(db, make_seller(db), handle="nairobithrift")
        start_verification(account)
        verify_via_oauth(account, "nairobithrift")

        assert account.verification_code is None


class TestTheSyncRail:
    """
    THE point of this module. An unproven claim must never produce a single
    product.
    """

    def test_an_unverified_account_cannot_be_synced(self, db: Session) -> None:
        account = make_account(db, make_seller(db))

        with pytest.raises(VerificationError, match="not verified"):
            require_syncable(account)

    def test_a_verified_account_can_be_synced(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        verify_via_oauth(account, account.handle)

        require_syncable(account)  # must not raise

    def test_a_disconnected_account_cannot_be_synced(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        verify_via_oauth(account, account.handle)
        account.is_active = False

        with pytest.raises(VerificationError, match="disconnected"):
            require_syncable(account)

    def test_can_sync_is_false_while_unverified(self, db: Session) -> None:
        assert make_account(db, make_seller(db)).can_sync is False

    def test_can_sync_is_true_once_verified_and_active(self, db: Session) -> None:
        account = make_account(db, make_seller(db))
        verify_via_oauth(account, account.handle)
        assert account.can_sync is True

    def test_can_sync_is_false_once_disconnected(self, db: Session) -> None:
        """Verified but disconnected still must not be scraped."""
        account = make_account(db, make_seller(db))
        verify_via_oauth(account, account.handle)
        account.is_active = False
        assert account.can_sync is False

    def test_a_new_account_starts_unverified(self, db: Session) -> None:
        """Unproven by default. Verification is opt-in work, never assumed."""
        account = make_account(db, make_seller(db))
        assert account.is_verified is False
        assert account.can_sync is False


class TestVerificationRails:
    """Database-level guards on the verification columns themselves."""

    def test_verified_without_a_method_is_refused(self, db: Session) -> None:
        """
        Otherwise we could not tell a bio-code proof from an OAuth one when
        auditing, or re-verify when a method is retired.
        """
        from sqlalchemy.exc import IntegrityError

        seller = make_seller(db)
        account = SocialAccount(
            seller_id=seller.id,
            platform=Platform.TIKTOK.value,
            handle="unmethodical",
            verified_at=datetime.now(UTC),
            verification_method=None,
        )
        db.add(account)
        with pytest.raises(IntegrityError, match="verified_has_method"):
            db.flush()

    def test_an_unknown_verification_method_is_refused(self, db: Session) -> None:
        from sqlalchemy.exc import IntegrityError

        seller = make_seller(db)
        account = SocialAccount(
            seller_id=seller.id,
            platform=Platform.TIKTOK.value,
            handle="dubious",
            verified_at=datetime.now(UTC),
            verification_method="vibes",
        )
        db.add(account)
        with pytest.raises(IntegrityError, match="verification_method_valid"):
            db.flush()
