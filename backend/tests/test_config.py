"""
Tests for typed settings.

The point of config.py is failing loudly and early, so most of these assert on
*how* it fails — that the message names the offending key, and that a
later-milestone key being absent does not stop the app from booting.

EVERY test here builds Settings with ``_env_file=None``. Without that, pydantic
reads the developer's real .env and the results depend on whose machine is
running — a test that passes for one person and fails for another is worse than
no test at all.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, settings

DB_URL = "postgresql://user:pass@localhost:5432/biashara"


def build(**overrides: object) -> Settings:
    """Settings isolated from the real .env, with a valid database by default."""
    values: dict[str, object] = {"database_url": DB_URL}
    values.update(overrides)
    # `_env_file` is a pydantic-settings init hook, not a declared field, so
    # mypy cannot see it. The ignore is narrow and deliberate.
    return Settings(_env_file=None, **values)  # type: ignore[call-arg,arg-type]


def test_accepts_a_minimal_p0_environment() -> None:
    assert build().database_url_str.startswith("postgresql+psycopg://")


def test_database_url_is_required() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "database_url" in str(exc.value)


def test_rejects_a_malformed_database_url() -> None:
    with pytest.raises(ValidationError):
        build(database_url="not-a-database-url")


def test_defaults_are_safe_for_local_development() -> None:
    s = build()
    assert s.app_env == "dev"
    assert s.is_prod is False
    assert s.gemini_model == "gemini-3.6-flash"
    assert s.apify_tiktok_actor_id == "clockworks~tiktok-scraper"


def test_base_url_never_keeps_a_trailing_slash() -> None:
    """A trailing slash produces `//path` when links are built by concatenation."""
    assert (
        build(app_base_url="https://biasharamall.com/").app_base_url == "https://biasharamall.com"
    )


def test_later_milestone_keys_are_optional() -> None:
    """P0 must boot with only a database — no Apify, Gemini, WhatsApp or Daraja."""
    s = build()
    assert s.apify_token is None
    assert s.gemini_api_key is None
    assert s.whatsapp_access_token is None
    assert s.daraja_consumer_key is None


def test_require_raises_a_useful_message_for_a_missing_key() -> None:
    with pytest.raises(RuntimeError) as exc:
        build().require("apify_token")

    message = str(exc.value)
    assert "APIFY_TOKEN" in message
    assert ".env" in message  # tells the reader where to fix it


def test_require_returns_a_present_value() -> None:
    assert build(apify_token="apify_api_xyz").require("apify_token") == "apify_api_xyz"


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Railway injects PORT and RAILWAY_* — deploys must not fail on them.

    Set as real environment variables rather than keyword arguments, because
    that is how they actually arrive and it is the path that must not break.
    """
    monkeypatch.setenv("DATABASE_URL", DB_URL)
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.app_env == "dev"
    assert s.database_url_str.startswith("postgresql+psycopg://")


class TestDriverNormalisation:
    """
    Providers hand out bare `postgresql://` URLs, which SQLAlchemy reads as
    "use psycopg2" — a driver we do not install. Normalising in config means
    .env holds the provider's URL verbatim on every environment.
    """

    def test_bare_postgresql_scheme_gets_the_psycopg_driver(self) -> None:
        assert build().database_url_str == (
            "postgresql+psycopg://user:pass@localhost:5432/biashara"
        )

    def test_an_explicit_psycopg_driver_is_left_alone(self) -> None:
        url = "postgresql+psycopg://user:pass@localhost:5432/biashara"
        assert build(database_url=url).database_url_str == url

    def test_test_database_url_is_normalised_too(self) -> None:
        s = build(test_database_url="postgresql://user:pass@localhost:5432/biashara_test")
        assert s.test_database_url_str == (
            "postgresql+psycopg://user:pass@localhost:5432/biashara_test"
        )

    def test_test_database_url_is_none_when_unset(self) -> None:
        assert build().test_database_url_str is None


def test_module_level_settings_loaded() -> None:
    """The imported singleton is usable — this is what the app actually runs on."""
    assert settings.app_name == "Biashara Mall"


class TestProductionRefusesDevelopmentDefaults:
    """
    A prod deploy that forgot its secrets must fail at startup, not later.

    SECRET_KEY is the serious one. Since W2 it signs sessions AND derives the
    key encrypting sellers' Daraja credentials — and its default is a literal
    string in a public repository. Booting on it would encrypt other people's
    payment credentials with a key anyone can read.

    A misconfiguration that boots happily is discovered by an incident. This one
    is discovered by a deploy log, before a seller has trusted it.
    """

    def test_prod_refuses_the_default_secret_key(self) -> None:
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            build(app_env="prod", app_base_url="https://shop.example.com")

    def test_prod_refuses_a_localhost_base_url(self) -> None:
        """It builds the M-Pesa callback. Pointing it at localhost means
        Safaricom posts the verdict into the void."""
        with pytest.raises(ValidationError, match="APP_BASE_URL"):
            build(
                app_env="prod",
                secret_key="a-real-long-random-value",
                app_base_url="http://localhost:8000",
            )

    def test_prod_boots_when_both_are_set(self) -> None:
        cfg = build(
            app_env="prod",
            secret_key="a-real-long-random-value",
            app_base_url="https://shop.example.com",
        )
        assert cfg.is_prod is True

    def test_development_still_boots_on_defaults(self) -> None:
        """The guard must not make local development harder."""
        assert build().is_prod is False


class TestMpesaKeysAcceptEitherPrefix:
    """
    Safaricom's API is called Daraja and its product is called M-Pesa, so both
    prefixes are in circulation.

    This is not cosmetic. ``extra="ignore"`` means a mismatched name is
    discarded in SILENCE — credentials that look configured and are not, which
    is the worst possible way to find out, on a live payment.
    """

    def test_the_daraja_prefix_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DARAJA_CONSUMER_KEY", "from-daraja")
        assert build().daraja_consumer_key == "from-daraja"

    def test_the_mpesa_prefix_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MPESA_CONSUMER_KEY", "from-mpesa")
        assert build().daraja_consumer_key == "from-mpesa"

    def test_the_environment_accepts_either_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MPESA_ENVIRONMENT", "production")
        assert build().daraja_environment == "production"

    def test_it_still_defaults_to_sandbox(self) -> None:
        """A deploy that sets neither cannot move real money."""
        assert build().daraja_environment == "sandbox"


class TestABlankDatabaseUrlIsNotAUrl:
    """
    A Railway variable reference that does not resolve produces whitespace, not
    an error — the container receives ``DATABASE_URL=' \n'``.

    Pydantic's own message for that is "Input should be a valid URL, relative
    URL without a base", which is true and useless: it describes the symptom and
    names no fix. These tests pin the message that replaces it, because a deploy
    that fails should say what to do about it.
    """

    def test_a_whitespace_url_names_the_variable_and_the_likely_cause(self) -> None:
        """The exact value seen in production when a reference did not resolve."""
        with pytest.raises(ValidationError, match="DATABASE_URL is set but empty"):
            build(database_url=" \n")

    def test_an_empty_url_fails_the_same_way(self) -> None:
        with pytest.raises(ValidationError, match="DATABASE_URL is set but empty"):
            build(database_url="")

    def test_the_message_points_at_the_service_name(self) -> None:
        """
        The fix is nearly always a mismatched service name in the reference, so
        the error says so rather than leaving it to be guessed.
        """
        with pytest.raises(ValidationError, match="same project"):
            build(database_url="   ")

    def test_surrounding_whitespace_is_stripped_rather_than_fatal(self) -> None:
        """
        A value pasted into a dashboard with a trailing newline is still a valid
        URL, and failing on it would be a defeat nobody can see in a text box.
        """
        cfg = build(database_url=f"  {DB_URL}\n")
        assert str(cfg.database_url).startswith("postgresql://")

    def test_a_blank_test_database_url_means_unset(self) -> None:
        """
        Unset is the correct state in production — the suite never runs there —
        and an empty box in a dashboard means the same thing as no box at all.
        Erroring on it would block a deploy over a variable nothing reads.
        """
        assert build(test_database_url="").test_database_url is None

    def test_a_blank_test_database_url_does_not_stop_the_app(self) -> None:
        """The regression this guards: an empty TEST_DATABASE_URL row in the
        Railway dashboard refusing to boot the whole service."""
        cfg = build(test_database_url="  \n")
        assert cfg.test_database_url is None
        assert cfg.test_database_url_str is None


class TestPastedCredentials:
    """
    Whitespace on a secret is invisible in a dashboard and fatal at runtime.

    We already lost a deploy to a ``DATABASE_URL`` of ``' \n'``. The Twilio auth
    token is worse: it is a shared HMAC secret, so one extra byte makes every
    genuine inbound message look forged, and the resulting 403 is
    indistinguishable from having pasted the wrong token entirely.
    """

    def test_a_token_pasted_with_a_newline_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TWILIO_AUTH_TOKEN", "  abc123def456  \n")

        assert build().twilio_auth_token == "abc123def456"

    def test_the_account_sid_and_number_are_trimmed_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123\n")
        monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", " +14155238886 ")

        settings = build()

        assert settings.twilio_account_sid == "AC123"
        assert settings.twilio_whatsapp_number == "+14155238886"

    def test_an_unset_token_stays_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        None must survive the validator.

        The webhook returns 503 when the token is None — refusing beats
        accepting unverifiable traffic. Coercing it to an empty string would
        make that check pass and every forged request reach the database.
        """
        monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)

        assert build().twilio_auth_token is None

    def test_a_base_url_with_a_trailing_newline_is_trimmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Signed webhooks recompute Twilio's signature from ``app_base_url``, so a
        stray character here rejects every real message with no visible cause.
        """
        monkeypatch.setenv("APP_BASE_URL", " https://example.up.railway.app/\n")

        assert build().app_base_url == "https://example.up.railway.app"
