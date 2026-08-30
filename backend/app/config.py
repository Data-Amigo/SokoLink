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

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    Field,
    PostgresDsn,
    ValidationError,
    field_validator,
    model_validator,
)
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

    #: The WhatsApp Business Account id. Distinct from the phone number id:
    #: templates belong to the ACCOUNT, messages are sent from the NUMBER, and
    #: one account can own several numbers. Needed only to create or list
    #: templates, never to send.
    whatsapp_business_account_id: str | None = None

    #: The approved template whose CTA button opens a shop INSIDE WhatsApp.
    #:
    #: WHY A TEMPLATE AT ALL, when the bot can already send a link. Meta's
    #: in-app browser opens links from CTA buttons on approved templates and
    #: from interactive messages. A link in a free-form reply — which is
    #: everything the bot says — is handed to the device's default browser
    #: instead. The template is the only shape that opens in WhatsApp.
    whatsapp_shop_template: str = "biashara_shop_link"

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
    # THE FOUR CREDENTIAL FIELDS BELOW ARE READ BY NOTHING. Verified by grep,
    # not assumed. Only `daraja_environment` has a job: it picks the sandbox or
    # production host.
    #
    # That is not an oversight, it is the architecture. An STK push deposits
    # into the shortcode that AUTHORISED it, so pushing with our credentials
    # would land every buyer's money in our Safaricom account and leave us
    # owing each seller theirs — a payment intermediary holding other people's
    # funds, which is the one thing this product refuses to be. The credentials
    # used for a real push are the SELLER's, encrypted on their PaymentMethod
    # and entered through the workspace.
    #
    # They are kept because they are a convenient place to hold sandbox values
    # while testing, and because deleting a documented name tends to produce a
    # deploy that sets it again. Setting them configures nothing.
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

    # ── Deployment identity ──────────────────────────────────────────────────
    #: The commit this container was built from, injected by Railway.
    #:
    #: WHY THIS IS WORTH A SETTING. "Is my code actually live?" has been
    #: answered wrongly more than once in this project — once because a stale
    #: worker held the port, once because a push went to a branch nobody
    #: deploys. Both times the only way to tell was to guess from behaviour.
    #: Reading it back from /health turns that into a fact.
    railway_git_commit_sha: str | None = None

    @property
    def version(self) -> str:
        """The running commit, short, or "unknown" outside a Railway build."""
        return (self.railway_git_commit_sha or "unknown")[:7]

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


#: What to print when the environment is empty. Named variables, in the order
#: they must be set, because a list of pydantic field errors does not tell
#: somebody staring at a dashboard which box to fill in.
_MISSING_ENV_HELP = """
DATABASE_URL is not set, so the application cannot start.

This is a DEPLOYMENT CONFIGURATION problem, not a code problem. The container
received no environment at all.

On Railway, check the SERVICE and the ENVIRONMENT selected at the top of the
page — variables belong to one service in one environment, and a second service
(or a second environment) starts with none. The "Suggested Variables" panel is
NOT your configuration: it is Railway reading .env.example and offering to
create those variables, and it reappears on every deploy while that file exists.

The variables this service needs:

    DATABASE_URL          the Postgres connection string
    SECRET_KEY            signs sessions; must not be the .env.example default
    APP_BASE_URL          https://<your-domain>   (no trailing slash)
    APP_ENV               prod          (NOT "production" — see app_env)
    TWILIO_ACCOUNT_SID    }
    TWILIO_AUTH_TOKEN     }  needed to receive WhatsApp messages
    TWILIO_WHATSAPP_NUMBER}
    GEMINI_API_KEY        needed to read forwarded catalogue posts
"""


#: The variables a deployed service is expected to carry. Names only — this
#: list is printed on a failed boot, and a secret in a log is a secret leaked.
EXPECTED_ENV = (
    "DATABASE_URL",
    "SECRET_KEY",
    "APP_ENV",
    "APP_BASE_URL",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_NUMBER",
    "GEMINI_API_KEY",
)


def _env_inventory() -> str:
    """
    Which expected variables the container actually received, by name.

    Returns:
        A block to append to the startup failure message.

    Notes:
        THIS IS THE LINE THAT ANSWERS "WHY DOES THIS KEEP HAPPENING". There is a
        large difference between one missing variable and an environment that is
        entirely empty, and the two have completely different causes:

            some present, one missing   somebody deleted or renamed a box
            NONE present                the deploy is running against a
                                        different SERVICE or ENVIRONMENT from
                                        the one the variables were set on

        Pydantic's ``input_value={}`` does say the second thing, but only to
        somebody who already knows to read it that way. This says it outright.

        NAMES ONLY, NEVER VALUES. This text goes into a deployment log that gets
        pasted into chats and screenshots.
    """
    present = [name for name in EXPECTED_ENV if os.environ.get(name, "").strip()]
    missing = [name for name in EXPECTED_ENV if name not in present]

    lines = ["", "What this container actually received:", ""]
    for name in EXPECTED_ENV:
        lines.append(f"    {'set    ' if name in present else 'MISSING'}  {name}")

    lines.append("")
    if not present:
        lines.append(
            "NONE of them are set. An empty environment almost always means the\n"
            "deploy is running against a different SERVICE or ENVIRONMENT from the\n"
            "one the variables were saved on — not that the values were lost."
        )
    elif missing:
        lines.append(
            f"{len(present)} of {len(EXPECTED_ENV)} are set, so the service does have an\n"
            "environment — the ones marked MISSING were never added, or were renamed."
        )
    return "\n".join(lines) + "\n"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Build the settings once per process.

    Cached rather than evaluated at import so that merely importing this module
    never reads the environment — which keeps tests and tooling able to import
    the app without a real .env present.

    Raises:
        RuntimeError: When required variables are missing, carrying a message
            that names the fix.

    Notes:
        WHY THIS CATCHES AND RE-RAISES. A MISSING variable never reaches the
        validators below — pydantic rejects the model first, and the container
        log reads:

            ValidationError: 1 validation error for Settings
            database_url
              Field required [type=missing, input_value={}, input_type=dict]

        That is accurate and nearly useless: it does not say which variable in
        which dashboard, and ``input_value={}`` — the fact that the WHOLE
        environment was empty — is the single most diagnostic thing in it and
        the easiest to miss. This has now cost two separate debugging sessions.

        The blank-string case is handled by a validator instead, because a value
        that is present but empty means something different: usually an
        unresolved ``${{Service.VAR}}`` reference rather than a missing box.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values come from env/.env
    except ValidationError as exc:
        if any(error["type"] == "missing" for error in exc.errors()):
            raise RuntimeError(_MISSING_ENV_HELP + _env_inventory()) from exc
        raise


settings = get_settings()
