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
from typing import Any, Literal

from pydantic import AliasChoices, Field, PostgresDsn, field_validator, model_validator
from pydantic_core.core_schema import ValidationInfo
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

    # ── WhatsApp: Meta Cloud API (not yet used) ──────────────────────────────
    whatsapp_phone_number_id: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_app_secret: str | None = None

    # ── WhatsApp: Twilio (used today, for sending the login code) ────────────
    # Twilio is the provider we can use NOW: sending needs no webhook and no
    # Meta business verification. Meta's Cloud API is cheaper at volume and
    # remains the likely destination — which is why both sets of keys live here
    # and everything goes through services/messaging.py rather than either SDK.
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None

    #: The WhatsApp-enabled number messages are sent FROM. Stored bare
    #: (254…) or with a +; the adapter normalises it either way.
    twilio_whatsapp_number: str | None = None

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

    @field_validator("database_url", "test_database_url", mode="before")
    @classmethod
    def _blank_url_is_not_a_url(cls, value: Any, info: ValidationInfo) -> Any:
        """
        Turn a blank or whitespace-only database URL into a message that helps.

        THIS EXISTS BECAUSE OF A REAL HOUR LOST. A Railway variable reference
        that does not resolve — a service renamed, or a typo in
        ``${{Postgres.DATABASE_URL}}`` — is not an error there. It expands to
        nothing, and the container receives ``DATABASE_URL=' \\n'``. Pydantic
        then reported:

            Input should be a valid URL, relative URL without a base
            [type=url_parsing, input_value=' \\n']

        which is true, and tells you nothing about what to do. A deploy that
        fails should name the fix, not the symptom.

        Whitespace is also stripped, so a value pasted with a trailing newline
        works rather than failing for a reason nobody can see in a dashboard.

        Args:
            value: The raw environment value.
            info: Carries the field name, for a message that names the variable.

        Returns:
            The trimmed URL; or None for a blank ``TEST_DATABASE_URL``, which is
            legitimately unset in production.

        Raises:
            ValueError: If ``DATABASE_URL`` is present but blank.
        """
        if not isinstance(value, str):
            return value

        trimmed = value.strip()
        if trimmed:
            return trimmed

        if info.field_name == "test_database_url":
            # Unset is the correct state in production, and an empty box in a
            # dashboard means the same thing as no box at all.
            return None

        raise ValueError(
            "DATABASE_URL is set but empty. On Railway this usually means a "
            "variable reference did not resolve — check the database service is "
            "named exactly as written in ${{Postgres.DATABASE_URL}} and lives in "
            "the same project."
        )

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
        """
        A trailing slash produces `//path` when links are built by concatenation.

        IT ALSO BREAKS EVERY INBOUND WEBHOOK. Twilio signs the exact URL it was
        given; we recompute that signature from ``app_base_url + path``. One
        stray slash makes every genuine message look forged, and the only
        symptom is a silent 403 in somebody else's dashboard.
        """
        return v.strip().rstrip("/")

    @field_validator("twilio_account_sid", "twilio_auth_token", "twilio_whatsapp_number")
    @classmethod
    def _strip_credential(cls, v: str | None) -> str | None:
        """
        Trim whitespace off a pasted credential.

        A TOKEN WITH A TRAILING NEWLINE IS INVISIBLE AND FATAL. It is a shared
        HMAC secret: one extra byte and every signature we compute differs from
        every signature Twilio sends, so every real message is rejected as a
        forgery. Dashboards show the value without revealing the whitespace, and
        the failure looks identical to having the wrong token entirely.

        We were bitten by exactly this on ``DATABASE_URL``. Same class of bug,
        same cheap fix.
        """
        return v.strip() if isinstance(v, str) else v

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
