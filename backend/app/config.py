"""
Typed application settings — the single door to the environment.

    .env / Railway variables ──> Settings (validated once) ──> settings

WHY this exists: reading ``os.environ`` scattered through a codebase means a
missing key fails at the moment it is first touched, which is usually deep
inside a request on production. Validating once at import gives one clear
failure, at startup, naming exactly what is wrong.

RULE: nothing outside this file reads ``os.environ``. Import ``settings``.

Keys are grouped by the milestone that introduces them. Later-milestone keys are
optional so the app boots on P0 with only a database configured; a feature that
needs its own key asserts it via ``require()``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, PostgresDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The repo root holds the single .env shared by the app, Alembic and the tests.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _with_psycopg_driver(dsn: str) -> str:
    """
    Force SQLAlchemy to use psycopg 3 rather than psycopg2.

    Railway (and every other provider) hands out a bare ``postgresql://`` URL.
    SQLAlchemy reads that as "use psycopg2", which we do not install — the
    result is a ModuleNotFoundError at engine creation, far from the cause.

    Normalising here means .env holds the provider's URL verbatim, with no
    hand-editing to remember on every environment.

    Args:
        dsn: A Postgres URL, with or without an explicit driver.

    Returns:
        The same URL with the ``+psycopg`` driver applied.
    """
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if dsn.startswith(prefix):
            # Already explicit — respect it rather than second-guessing.
            return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgres://"):
        # Some providers still emit the legacy scheme.
        return dsn.replace("postgres://", "postgresql+psycopg://", 1)
    return dsn


#: The placeholder secret key. Named as a constant so the production guard
#: below and the default can never drift apart — a guard comparing against
#: a copy of the string would stop working the moment one was edited.
DEV_SECRET_KEY = "dev-only-insecure-key-change-me"


class Settings(BaseSettings):
    """Every environment value the application is allowed to read."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Railway injects variables we do not model (PORT, RAILWAY_*). Ignoring
        # them keeps deploys from failing on someone else's config.
        extra="ignore",
    )

    # ── P0: application ──────────────────────────────────────────────────────
    app_name: str = "Biashara Mall"
    app_env: Literal["dev", "test", "prod"] = "dev"

    #: Public base URL, no trailing slash. Used to build storefront links.
    app_base_url: str = "http://localhost:8000"

    # ── P0: database ─────────────────────────────────────────────────────────
    #: Full Postgres URL. Required — the app cannot do anything without it.
    database_url: PostgresDsn

    #: Separate database used by the test suite.
    #:
    #: Tests create tables and write rows, so they must NEVER run against the
    #: application database. Kept as its own key rather than derived, so
    #: pointing tests at real data has to be a deliberate act rather than an
    #: accident. When unset, database-backed tests skip instead of running
    #: somewhere dangerous.
    test_database_url: PostgresDsn | None = None

    # ── P1: ingestion (Apify) ────────────────────────────────────────────────
    apify_token: str | None = None
    apify_tiktok_actor_id: str = "clockworks~tiktok-scraper"

    # ── P1: AI (Gemini) ──────────────────────────────────────────────────────
    gemini_api_key: str | None = None

    #: Vision + reasoning model for the price cascade.
    #:
    #: gemini-2.5-flash was retired for new API keys (404 NOT_FOUND, 2026-08-08).
    #: 3.6-flash is current and is also what Project TIKTOK validated on for
    #: Sheng/Swahili in-image text, which is the hard part of this job.
    gemini_model: str = "gemini-3.6-flash"

    # ── P2: WhatsApp Cloud API ───────────────────────────────────────────────
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_app_secret: str | None = None

    # ── W2: M-Pesa Daraja ────────────────────────────────────────────────────
    # NOTE: in production the credentials used for an STK push are the SELLER's,
    # stored encrypted on their PaymentMethod — not these. We are never in the
    # money path. The keys below exist only for our own sandbox testing.
    #
    # EACH ACCEPTS TWO NAMES. Safaricom's API is called Daraja and its product is
    # called M-Pesa, so both prefixes are in circulation and a .env written from
    # either habit is correct. With `extra="ignore"` a mismatched name would be
    # discarded in silence — credentials that look configured and are not, which
    # is the worst way to find out on a live payment.
    daraja_consumer_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DARAJA_CONSUMER_KEY", "MPESA_CONSUMER_KEY"),
    )
    daraja_consumer_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DARAJA_CONSUMER_SECRET", "MPESA_CONSUMER_SECRET"),
    )
    daraja_shortcode: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DARAJA_SHORTCODE", "MPESA_SHORTCODE"),
    )
    daraja_passkey: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DARAJA_PASSKEY", "MPESA_PASSKEY"),
    )

    #: Which Daraja host to call. Defaults to sandbox so a deploy that forgets
    #: to set it cannot move real money; production is opted into, never
    #: inherited.
    daraja_environment: Literal["sandbox", "production"] = Field(
        default="sandbox",
        validation_alias=AliasChoices("DARAJA_ENVIRONMENT", "MPESA_ENVIRONMENT"),
    )

    # ── Security ─────────────────────────────────────────────────────────────
    #: Signs sessions AND derives the key that encrypts sellers' Daraja
    #: credentials — see app/secrets_vault.py. Refused in prod if left at the
    #: default; see the validator below.
    secret_key: str = Field(default=DEV_SECRET_KEY)

    @model_validator(mode="after")
    def _production_must_not_use_development_defaults(self) -> Settings:
        """
        Refuse to boot a production deploy that is still on dev placeholders.

        THE SECRET KEY IS THE SERIOUS ONE. It signs sessions and, since W2, also
        derives the key that encrypts sellers' Daraja credentials. Its default
        value is a literal string in a public repository — so a prod deploy that
        forgot to set it would encrypt other people's payment credentials with a
        key anyone can read, and every session cookie would be forgeable.

        Failing at startup is deliberate. A misconfiguration that boots happily
        and is discovered later is discovered by an incident; this one is
        discovered by a deploy log, before a single seller has trusted it.

        Raises:
            ValueError: If ``APP_ENV=prod`` and either the secret key or the
                base URL is still a development placeholder.
        """
        if self.app_env != "prod":
            return self

        if self.secret_key == DEV_SECRET_KEY:
            raise ValueError(
                "SECRET_KEY is still the development default. Set a long random "
                "value in production — it signs sessions and encrypts sellers' "
                "M-Pesa credentials, and changing it later invalidates both."
            )

        if "localhost" in self.app_base_url or "127.0.0.1" in self.app_base_url:
            raise ValueError(
                "APP_BASE_URL still points at localhost. Set it to the public "
                "URL — it builds the M-Pesa callback and every link preview."
            )

        return self

    @field_validator("app_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        """A trailing slash produces `//path` when links are built by concatenation."""
        return v.rstrip("/")

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def database_url_str(self) -> str:
        """The application DSN, driver-normalised for SQLAlchemy."""
        return _with_psycopg_driver(str(self.database_url))

    @property
    def test_database_url_str(self) -> str | None:
        """The test DSN, driver-normalised. ``None`` when unset."""
        if self.test_database_url is None:
            return None
        return _with_psycopg_driver(str(self.test_database_url))

    def require(self, field: str) -> str:
        """
        Assert an optional key is present, for a feature that needs it.

        Lets later milestones depend on their own keys without making the whole
        app refuse to boot before those keys exist.

        Args:
            field: Attribute name on this Settings object.

        Returns:
            The value, guaranteed non-empty.

        Raises:
            RuntimeError: If the key is unset or empty. The message names the
                key and the file to set it in, because the person hitting this
                is usually mid-task and does not want to go reading source.
        """
        value = getattr(self, field, None)
        if not value:
            raise RuntimeError(
                f"{field.upper()} is not set. This feature needs it — "
                f"add it to .env (see .env.example for where to get the value)."
            )
        return str(value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Build the settings once per process.

    Cached rather than evaluated at import so that merely importing this module
    never reads the environment — which keeps tests and tooling able to import
    the app without a real .env present.
    """
    return Settings()  # type: ignore[call-arg]  # values come from env/.env


settings = get_settings()
