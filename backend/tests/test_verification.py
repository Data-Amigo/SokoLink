"""
Tests for social account ownership verification.

The attack this defends against is specific: a stranger types someone else's
handle, we scrape her videos and photos, and they publish a storefront pointing
at THEIR WhatsApp number. Sales diversion, invisible to the buyer.

THE LOAD-BEARING TESTS are in `TestOnlyVerifiedAccountsExist`. Everything else
supports them. The design goal is that an unverified connection is not merely
disallowed but *unrepresentable* — the database refuses to hold one.

The scraper is faked throughout. No test calls a paid API.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AccountClaim, Platform, SocialAccount, VerificationMethod
from app.models.account_claim import MAX_ATTEMPTS
from app.schemas.tiktok import ScrapedProfile, TikTokAuthor
from app.services.scraper import ScraperError
from app.services.verification import (
    CODE_PREFIX,
    VerificationError,
    check_claim,
    complete_via_oauth,
    generate_code,
    purge_expired_claims,
    require_syncable,
    start_claim,
)
from tests.factories import make_seller


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
                {
                    "name": handle,
                    "nickName": "Nairobi Thrift",
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


def claim_for(db: Session, **overrides: Any) -> AccountClaim:
    """A pending claim on @nairobithrift for a fresh seller."""
    seller = overrides.pop("seller", None) or make_seller(db)
    return start_claim(
        db,
        seller_id=seller.id,
        platform=overrides.pop("platform", Platform.TIKTOK),
        handle=overrides.pop("handle", "nairobithrift"),
    )


class TestOnlyVerifiedAccountsExist:
    """
    The core guarantee: a SocialAccount row IS a verified account.

    Not "should be" — cannot be otherwise. The columns are NOT NULL and one
    function creates them, so no query downstream has to remember to filter.
    """

    def test_an_unverified_account_cannot_be_stored_at_all(self, db: Session) -> None:
        seller = make_seller(db)
        db.add(
            SocialAccount(
                seller_id=seller.id,
                platform=Platform.TIKTOK.value,
                handle="unproven",
                verified_at=None,
                verification_method=None,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()

    def test_an_account_without_a_method_cannot_be_stored(self, db: Session) -> None:
        seller = make_seller(db)
        db.add(
            SocialAccount(
                seller_id=seller.id,
                platform=Platform.TIKTOK.value,
                handle="methodless",
                verified_at=datetime.now(UTC),
                verification_method=None,
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()

    def test_an_unknown_method_cannot_be_stored(self, db: Session) -> None:
        seller = make_seller(db)
        db.add(
            SocialAccount(
                seller_id=seller.id,
                platform=Platform.TIKTOK.value,
                handle="dubious",
                verified_at=datetime.now(UTC),
                verification_method="vibes",
            )
        )
        with pytest.raises(IntegrityError, match="verification_method_valid"):
            db.flush()

    def test_starting_a_claim_connects_nothing(self, db: Session) -> None:
        """A claim is a waiting room. It grants no access to anything."""
        seller = make_seller(db)
        start_claim(db, seller_id=seller.id, platform=Platform.TIKTOK, handle="nairobithrift")

        assert seller.connected_platforms == []
        assert seller.has_any_connection is False
        assert db.query(SocialAccount).count() == 0

    def test_a_failed_check_still_connects_nothing(self, db: Session) -> None:
        claim = claim_for(db)
        assert check_claim(db, claim, FakeScraper(bio="no code here")) is None
        assert db.query(SocialAccount).count() == 0


class TestCodeGeneration:
    def test_a_code_is_prefixed_and_recognisable(self) -> None:
        assert generate_code().startswith(CODE_PREFIX)

    def test_codes_are_unique(self) -> None:
        assert len({generate_code() for _ in range(200)}) > 190

    def test_codes_avoid_characters_that_are_easy_to_mistype(self) -> None:
        """0/O and 1/I/l are exactly where retyping on a phone goes wrong."""
        for _ in range(100):
            body = generate_code().removeprefix(CODE_PREFIX)
            assert not (set(body) & set("O0I1L"))


class TestStartingAClaim:
    def test_issues_a_code_and_an_expiry(self, db: Session) -> None:
        claim = claim_for(db)
        assert claim.code.startswith(CODE_PREFIX)
        assert claim.is_expired is False

    def test_the_handle_is_normalised(self, db: Session) -> None:
        seller = make_seller(db)
        claim = start_claim(
            db, seller_id=seller.id, platform=Platform.TIKTOK, handle="@NairobiThrift"
        )
        assert claim.handle == "nairobithrift"

    def test_an_empty_handle_is_refused(self, db: Session) -> None:
        seller = make_seller(db)
        with pytest.raises(VerificationError, match="enter the account handle"):
            start_claim(db, seller_id=seller.id, platform=Platform.TIKTOK, handle="  @ ")

    def test_claiming_again_replaces_the_old_claim(self, db: Session) -> None:
        """Asking again means the code was lost. Reusing extends its life."""
        seller = make_seller(db)
        first = claim_for(db, seller=seller)
        first_code = first.code
        second = claim_for(db, seller=seller)

        assert second.code != first_code
        assert db.query(AccountClaim).filter_by(seller_id=seller.id).count() == 1

    def test_a_handle_already_verified_elsewhere_is_refused(self, db: Session) -> None:
        """The whole point — you cannot claim what someone else has proven."""
        first = claim_for(db)
        check_claim(db, first, FakeScraper(bio=first.code))

        other = make_seller(db, slug="other-shop")
        with pytest.raises(VerificationError, match="another SokoLink shop"):
            start_claim(db, seller_id=other.id, platform=Platform.TIKTOK, handle="nairobithrift")

    def test_reclaiming_your_own_account_says_so_plainly(self, db: Session) -> None:
        seller = make_seller(db)
        claim = claim_for(db, seller=seller)
        check_claim(db, claim, FakeScraper(bio=claim.code))

        with pytest.raises(VerificationError, match="already connected to your shop"):
            start_claim(db, seller_id=seller.id, platform=Platform.TIKTOK, handle="nairobithrift")


class TestCheckingAClaim:
    def test_a_matching_bio_creates_the_account(self, db: Session) -> None:
        claim = claim_for(db)
        account = check_claim(db, claim, FakeScraper(bio=f"my shop {claim.code}"))

        assert account is not None
        assert account.handle == "nairobithrift"
        assert account.verification_method == VerificationMethod.BIO_CODE.value

    def test_the_claim_is_deleted_once_used(self, db: Session) -> None:
        claim = claim_for(db)
        check_claim(db, claim, FakeScraper(bio=claim.code))
        assert db.query(AccountClaim).count() == 0

    def test_the_profile_is_auto_filled(self, db: Session) -> None:
        """The seller confirms rather than types."""
        claim = claim_for(db)
        account = check_claim(db, claim, FakeScraper(bio=claim.code))

        assert account is not None
        assert account.display_name == "Nairobi Thrift"
        assert account.follower_count == 22000
        assert account.post_count == 1453

    def test_matching_ignores_case(self, db: Session) -> None:
        """Phone keyboards auto-capitalise; rejecting that is needless friction."""
        claim = claim_for(db)
        assert check_claim(db, claim, FakeScraper(bio=claim.code.upper())) is not None

    def test_a_code_buried_in_a_real_bio_is_found(self, db: Session) -> None:
        """Real bios are full of phone numbers — spike 02 confirmed."""
        claim = claim_for(db)
        bio = f"zuma bales and LADIESWEAr contact us 0105515839/0754234636 {claim.code}"
        assert check_claim(db, claim, FakeScraper(bio=bio)) is not None

    def test_a_missing_code_returns_none_rather_than_raising(self, db: Session) -> None:
        """An ordinary outcome — the seller probably has not saved their bio."""
        claim = claim_for(db)
        assert check_claim(db, claim, FakeScraper(bio="nothing here")) is None

    def test_an_empty_bio_returns_none(self, db: Session) -> None:
        claim = claim_for(db)
        assert check_claim(db, claim, FakeScraper(bio=None)) is None

    def test_someone_elses_code_does_not_verify(self, db: Session) -> None:
        claim = claim_for(db)
        assert check_claim(db, claim, FakeScraper(bio=generate_code())) is None

    def test_an_expired_claim_is_refused(self, db: Session) -> None:
        claim = claim_for(db)
        claim.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        with pytest.raises(VerificationError, match="expired"):
            check_claim(db, claim, FakeScraper(bio=claim.code))

    def test_an_expired_claim_costs_no_paid_scrape(self, db: Session) -> None:
        claim = claim_for(db)
        claim.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        scraper = FakeScraper(bio=claim.code)

        with pytest.raises(VerificationError):
            check_claim(db, claim, scraper)
        assert scraper.calls == 0

    def test_attempts_are_counted(self, db: Session) -> None:
        claim = claim_for(db)
        check_claim(db, claim, FakeScraper(bio="no"))
        check_claim(db, claim, FakeScraper(bio="still no"))
        assert claim.attempts == 2

    def test_attempts_are_capped_because_each_one_is_billable(self, db: Session) -> None:
        claim = claim_for(db)
        claim.attempts = MAX_ATTEMPTS
        scraper = FakeScraper(bio=claim.code)

        with pytest.raises(VerificationError, match="Too many attempts"):
            check_claim(db, claim, scraper)
        assert scraper.calls == 0, "the cap must be checked BEFORE the paid call"

    def test_a_scrape_failure_explains_itself(self, db: Session) -> None:
        """A private profile and a rate limit need different responses."""
        claim = claim_for(db)
        with pytest.raises(VerificationError, match="private"):
            check_claim(db, claim, FakeScraper(fail="profile may be private"))


class TestOAuthPath:
    def test_a_matching_handle_connects_the_account(self, db: Session) -> None:
        claim = claim_for(db)
        account = complete_via_oauth(db, claim, "nairobithrift")

        assert account.verification_method == VerificationMethod.OAUTH.value
        assert db.query(AccountClaim).count() == 0

    def test_the_at_sign_and_case_are_tolerated(self, db: Session) -> None:
        claim = claim_for(db)
        assert complete_via_oauth(db, claim, "@NairobiThrift") is not None

    def test_signing_in_as_someone_else_is_refused(self, db: Session) -> None:
        """
        The value of OAuth: the handle comes from the PROVIDER, never from
        anything the seller typed. A mismatch means a different account.
        """
        claim = claim_for(db)
        with pytest.raises(VerificationError, match="signed in as"):
            complete_via_oauth(db, claim, "someoneelse")
        assert db.query(SocialAccount).count() == 0


class TestSyncRail:
    def test_a_connected_account_can_sync(self, db: Session) -> None:
        claim = claim_for(db)
        account = check_claim(db, claim, FakeScraper(bio=claim.code))
        assert account is not None
        require_syncable(account)  # must not raise

    def test_a_disconnected_account_cannot_sync(self, db: Session) -> None:
        claim = claim_for(db)
        account = check_claim(db, claim, FakeScraper(bio=claim.code))
        assert account is not None
        account.is_active = False

        with pytest.raises(VerificationError, match="disconnected"):
            require_syncable(account)


class TestClaimHousekeeping:
    def test_expired_claims_are_purged(self, db: Session) -> None:
        """
        An expired claim grants nothing but holds the (seller, platform) slot,
        so a seller who abandoned an attempt would be blocked from a fresh one.
        """
        claim = claim_for(db)
        claim.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db.flush()

        assert purge_expired_claims(db) == 1
        assert db.query(AccountClaim).count() == 0

    def test_live_claims_survive_a_purge(self, db: Session) -> None:
        claim_for(db)
        assert purge_expired_claims(db) == 0
        assert db.query(AccountClaim).count() == 1

    def test_two_sellers_may_claim_the_same_handle_until_one_proves_it(self, db: Session) -> None:
        """
        Racing claims are fine — neither grants anything. Only verification
        decides, and the unique constraint settles the race.
        """
        first = make_seller(db, slug="first-shop")
        second = make_seller(db, slug="second-shop")

        claim_a = start_claim(db, seller_id=first.id, platform=Platform.TIKTOK, handle="contested")
        start_claim(db, seller_id=second.id, platform=Platform.TIKTOK, handle="contested")

        assert check_claim(db, claim_a, FakeScraper(bio=claim_a.code)) is not None
        assert db.query(SocialAccount).filter_by(handle="contested").count() == 1
