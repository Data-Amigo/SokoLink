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
