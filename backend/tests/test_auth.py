"""
Tests for password hashing, session tokens, signup and login.

The most important tests here are the ANTI-ENUMERATION ones. A stranger must
not be able to learn which email addresses are registered — not from the error
message, and not from how long the response takes. Both are asserted below,
because both have been real vulnerabilities in real products.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy.orm import Session

from app.models import Account, Seller
from app.security import (
    DUMMY_HASH,
    SESSION_LIFETIME,
    AuthError,
    create_session_token,
    hash_password,
    read_session_token,
    validate_password_strength,
    verify_password,
)
from app.services.accounts import (
    RESERVED_SLUGS,
    SignupError,
    authenticate,
    create_account,
    get_account,
    reserve_slug,
    slugify,
)

GOOD_PASSWORD = "correct-horse-battery"


def signup(db: Session, **overrides: object) -> Account:
    """A registered account, with fields overridable per test."""
    values: dict[str, object] = {
        "email": "guru@example.com",
        "password": GOOD_PASSWORD,
        "shop_name": "Nairobi Thrift",
    }
    values.update(overrides)
    return create_account(db, **values)  # type: ignore[arg-type]


class TestPasswordHashing:
    def test_a_hash_never_contains_the_password(self) -> None:
        assert GOOD_PASSWORD not in hash_password(GOOD_PASSWORD)

    def test_the_same_password_hashes_differently_each_time(self) -> None:
        """Salting. Identical hashes would reveal which users share a password."""
        assert hash_password(GOOD_PASSWORD) != hash_password(GOOD_PASSWORD)

    def test_the_right_password_verifies(self) -> None:
        assert verify_password(GOOD_PASSWORD, hash_password(GOOD_PASSWORD)) is True

    def test_the_wrong_password_does_not(self) -> None:
        assert verify_password("wrong", hash_password(GOOD_PASSWORD)) is False

    def test_a_malformed_hash_returns_false_rather_than_raising(self) -> None:
        """A corrupt row must fail login, not 500 the whole request."""
        assert verify_password(GOOD_PASSWORD, "not-a-real-hash") is False

    def test_the_dummy_hash_is_a_real_hash(self) -> None:
        """
        It has to be genuinely verifiable, or the timing equaliser would return
        early and defeat its own purpose.
        """
        assert verify_password("anything at all", DUMMY_HASH) is False


class TestPasswordStrength:
    def test_a_long_password_is_accepted(self) -> None:
        validate_password_strength(GOOD_PASSWORD)

    def test_a_short_password_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 8"):
            validate_password_strength("short")

    def test_a_long_passphrase_needs_no_special_characters(self) -> None:
        """Length beats character-class rules, especially on a phone keyboard."""
        validate_password_strength("my shop sells very nice sandals")


class TestSessionTokens:
    def test_a_token_round_trips(self) -> None:
        assert read_session_token(create_session_token(42)) == 42

    def test_a_tampered_token_is_rejected(self) -> None:
        token = create_session_token(42)
        with pytest.raises(AuthError):
            read_session_token(token[:-4] + "aaaa")

    def test_garbage_is_rejected(self) -> None:
        with pytest.raises(AuthError):
            read_session_token("not.a.token")

    def test_a_token_signed_with_another_key_is_rejected(self) -> None:
        """The signature is what stops anyone minting themselves a session."""
        import jwt

        forged = jwt.encode({"sub": "42"}, "some-other-secret", algorithm="HS256")
        with pytest.raises(AuthError):
            read_session_token(forged)

    def test_an_expired_token_is_rejected(self) -> None:
        import datetime

        import jwt

        from app.config import settings

        past = datetime.datetime.now(datetime.UTC) - SESSION_LIFETIME - datetime.timedelta(days=1)
        expired = jwt.encode({"sub": "42", "exp": past}, settings.secret_key, algorithm="HS256")
        with pytest.raises(AuthError):
            read_session_token(expired)


class TestSlugs:
    def test_a_shop_name_becomes_a_slug(self) -> None:
        assert slugify("Nairobi Thrift") == "nairobi-thrift"

    def test_punctuation_and_case_are_stripped(self) -> None:
        assert slugify("ZUMA Mitumba Bales!!") == "zuma-mitumba-bales"

    def test_accents_are_folded_not_dropped(self) -> None:
        """ "Café" must become "cafe", never "caf"."""
        assert slugify("Café Shop") == "cafe-shop"

    def test_a_collision_gets_a_suffix(self, db: Session) -> None:
        """
        A taken shop name must not block signup. The slug is not permanent
        until the seller publishes.
        """
        signup(db)
        assert reserve_slug(db, "Nairobi Thrift") == "nairobi-thrift-2"

    def test_reserved_words_are_refused(self, db: Session) -> None:
        """Otherwise a seller could take /login or impersonate the platform."""
        with pytest.raises(SignupError):
            reserve_slug(db, "admin")

    def test_every_reserved_word_is_lowercase_and_usable(self) -> None:
        """Guards a typo in the reserved list from silently disabling a guard."""
        for word in RESERVED_SLUGS:
            assert word == word.lower()
            assert slugify(word) == word

    def test_a_name_with_no_usable_characters_is_refused(self, db: Session) -> None:
        with pytest.raises(SignupError, match="at least three"):
            reserve_slug(db, "!!!")


class TestSignup:
    def test_creates_an_account_and_its_shop_together(self, db: Session) -> None:
        """A login with no shop is a dead end, so both exist or neither does."""
        account = signup(db)

        assert account.seller is not None
        assert account.seller.slug == "nairobi-thrift"
        assert account.seller.display_name == "Nairobi Thrift"

    def test_the_email_is_lowercased(self, db: Session) -> None:
        account = signup(db, email="Guru@Example.COM")
        assert account.email == "guru@example.com"

    def test_the_password_is_never_stored_in_plaintext(self, db: Session) -> None:
        account = signup(db)
        assert GOOD_PASSWORD not in account.password_hash

    def test_a_duplicate_email_is_refused(self, db: Session) -> None:
        signup(db)
        with pytest.raises(SignupError, match="already exists"):
            signup(db, shop_name="Another Shop")

    def test_a_duplicate_email_in_different_case_is_refused(self, db: Session) -> None:
        """Otherwise Guru@x.com and guru@x.com become two accounts."""
        signup(db, email="guru@example.com")
        with pytest.raises(SignupError, match="already exists"):
            signup(db, email="GURU@example.com", shop_name="Another Shop")

    def test_a_weak_password_is_refused(self, db: Session) -> None:
        with pytest.raises(SignupError, match="at least 8"):
            signup(db, password="short")

    def test_a_malformed_email_is_refused(self, db: Session) -> None:
        with pytest.raises(SignupError, match="valid email"):
            signup(db, email="not-an-email")

    def test_an_empty_shop_name_is_refused(self, db: Session) -> None:
        with pytest.raises(SignupError, match="shop name"):
            signup(db, shop_name="   ")

    def test_a_new_shop_starts_unpublished(self, db: Session) -> None:
        """Nothing goes live before the seller has reviewed their drafts."""
        account = signup(db)
        assert account.seller is not None
        assert account.seller.is_published is False


class TestAuthentication:
    def test_correct_credentials_return_the_account(self, db: Session) -> None:
        created = signup(db)
        assert (
            authenticate(db, identifier="guru@example.com", password=GOOD_PASSWORD).id == created.id
        )

    def test_the_email_match_is_case_insensitive(self, db: Session) -> None:
        signup(db)
        assert authenticate(db, identifier="GURU@Example.com", password=GOOD_PASSWORD) is not None

    def test_a_wrong_password_is_rejected(self, db: Session) -> None:
        signup(db)
        with pytest.raises(AuthError):
            authenticate(db, identifier="guru@example.com", password="wrong-password")

    def test_a_deactivated_account_cannot_log_in(self, db: Session) -> None:
        account = signup(db)
        account.is_active = False
        db.flush()

        with pytest.raises(AuthError):
            authenticate(db, identifier="guru@example.com", password=GOOD_PASSWORD)

    def test_login_records_the_time(self, db: Session) -> None:
        signup(db)
        account = authenticate(db, identifier="guru@example.com", password=GOOD_PASSWORD)
        assert account.last_login_at is not None


class TestAntiEnumeration:
    """
    A stranger must not learn which emails are registered — not from the
    message, and not from the timing. Both have been real vulnerabilities in
    real products.
    """

    def test_unknown_email_and_wrong_password_give_the_same_message(self, db: Session) -> None:
        signup(db)

        with pytest.raises(AuthError) as unknown:
            authenticate(db, identifier="nobody@example.com", password=GOOD_PASSWORD)
        with pytest.raises(AuthError) as wrong:
            authenticate(db, identifier="guru@example.com", password="wrong-password")

        assert str(unknown.value) == str(wrong.value)

    def test_a_deactivated_account_gives_that_same_message(self, db: Session) -> None:
        """ "Your account is disabled" would confirm the address exists."""
        account = signup(db)
        account.is_active = False
        db.flush()

        with pytest.raises(AuthError) as disabled:
            authenticate(db, identifier="guru@example.com", password=GOOD_PASSWORD)
        with pytest.raises(AuthError) as unknown:
            authenticate(db, identifier="nobody@example.com", password=GOOD_PASSWORD)

        assert str(disabled.value) == str(unknown.value)

    def test_an_unknown_email_costs_similar_time_to_a_wrong_password(self, db: Session) -> None:
        """
        The timing oracle. Without the DUMMY_HASH comparison, a missing email
        returns almost instantly while a real one pays for Argon2 — and that
        difference is measurable over the network.

        The bound is loose on purpose: this asserts the hash actually happens,
        not a precise duration, because CI machines are noisy.
        """
        signup(db)

        start = time.perf_counter()
        with pytest.raises(AuthError):
            authenticate(db, identifier="nobody@example.com", password=GOOD_PASSWORD)
        unknown_email = time.perf_counter() - start

        start = time.perf_counter()
        with pytest.raises(AuthError):
            authenticate(db, identifier="guru@example.com", password="wrong-password")
        wrong_password = time.perf_counter() - start

        # An unknown email must not be an order of magnitude faster.
        assert unknown_email > wrong_password / 10


class TestGetAccount:
    def test_returns_an_active_account(self, db: Session) -> None:
        created = signup(db)
        assert get_account(db, created.id) is not None

    def test_returns_none_for_a_deactivated_account(self, db: Session) -> None:
        """A live session must stop working the moment an account is disabled."""
        created = signup(db)
        created.is_active = False
        db.flush()
        assert get_account(db, created.id) is None

    def test_returns_none_for_an_unknown_id(self, db: Session) -> None:
        assert get_account(db, 999_999) is None


class TestAccountModel:
    def test_repr_never_leaks_the_email(self, db: Session) -> None:
        """repr() lands in logs and tracebacks."""
        account = signup(db)
        assert "example.com" not in repr(account)

    def test_deleting_an_account_deletes_its_shop(self, db: Session) -> None:
        account = signup(db)
        seller_id = account.seller.id if account.seller else None

        db.delete(account)
        db.flush()

        assert db.get(Seller, seller_id) is None
