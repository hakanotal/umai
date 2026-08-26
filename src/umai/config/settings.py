"""Application settings, env-driven, via pydantic-settings.

Mirrors .env.example. The only two values that genuinely differ between the Mac
and Railway are the Telegram mode and the database URL; every divergence beyond
those is a bug that only appears after deploy.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Query parameters libpq understands and asyncpg does not. asyncpg rejects an
# unknown keyword rather than ignoring it, so leaving one in place is a
# connection error at startup, not a warning.
_LIBPQ_ONLY_PARAMS = ("sslmode=", "channel_binding=", "target_session_attrs=")


def normalise_async_dsn(url: str) -> str:
    """Rewrite a libpq-style DSN into one asyncpg can actually open.

    Railway, like most managed Postgres, publishes `postgresql://...` and
    spells its options the way libpq spells them. SQLAlchemy needs the driver
    named in the scheme, and asyncpg refuses `sslmode` outright, so a DSN
    pasted straight from the provider fails twice over: once in
    `create_async_engine`, and again in alembic's `env.py`, which reads the
    environment variable directly and would otherwise be a second place to
    remember.

    Deliberately not a rewrite of the whole URL through urllib: the password
    is in there, percent-encoded, and round-tripping it through a parser is a
    way to corrupt a credential for no gain. Scheme and query are the only two
    parts that need touching.
    """
    if not url:
        return url

    scheme, sep, rest = url.partition("://")
    if sep and scheme in {"postgres", "postgresql"}:
        url = f"postgresql+asyncpg://{rest}"

    head, sep, query = url.partition("?")
    if not sep:
        return url
    kept = [part for part in query.split("&") if part and not part.startswith(_LIBPQ_ONLY_PARAMS)]
    return f"{head}?{'&'.join(kept)}" if kept else head


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env first, then .local.env so a local override wins (later entries
        # override earlier ones via dict.update in pydantic-settings v2).
        env_file=(".env", ".local.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "prod"] = Field(default="dev", alias="UMAI_ENV")

    # --- Telegram ----------------------------------------------------------
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_webhook_url: str = Field(default="", alias="TELEGRAM_WEBHOOK_URL")
    telegram_webhook_secret: str = Field(default="", alias="TELEGRAM_WEBHOOK_SECRET")

    # --- Access ------------------------------------------------------------
    invite_phrase: str = Field(default="", alias="UMAI_INVITE_CODE")
    # The one id that skips the phrase. Without it the first run of a fresh
    # deployment has nobody who can admit anybody, including themselves.
    bootstrap_admin_telegram_id: int | None = Field(
        default=None, alias="UMAI_BOOTSTRAP_ADMIN_TELEGRAM_ID"
    )

    # --- Models ------------------------------------------------------------
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")

    # --- Database ----------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://umai:dev@localhost:5433/umai",
        alias="DATABASE_URL",
    )

    # --- Ingest and media --------------------------------------------------
    health_ingest_token: str = Field(default="", alias="HEALTH_INGEST_TOKEN")
    media_dir: Path = Field(default=Path("./data/media"), alias="MEDIA_DIR")
    http_host: str = Field(default="127.0.0.1", alias="UMAI_HTTP_HOST")
    # `PORT` is the fallback because that is the name Railway injects, and a
    # platform-assigned port is not something anybody should have to copy into
    # a second variable by hand. UMAI_HTTP_PORT still wins when both are set,
    # so a local override survives being run somewhere that also sets PORT.
    http_port: int = Field(
        default=8000,
        validation_alias=AliasChoices("UMAI_HTTP_PORT", "PORT"),
    )

    # --- Locale ------------------------------------------------------------
    tz: str = Field(default="", alias="TZ")

    # --- Defaults for a new person -----------------------------------------
    # Genuinely a system-wide default rather than somebody's data: everyone
    # starts at 2500 ml and can change it in chat. Contrast the fields that
    # used to live here — sex, height, birth date, goal rate, starting weight,
    # cuisines — which were one person's body and are now columns on `users`,
    # filled in by the wizard.
    water_target_ml: float = Field(default=2500.0, alias="UMAI_WATER_TARGET_ML")

    @field_validator("database_url")
    @classmethod
    def _asyncpg_dsn(cls, v: str) -> str:
        """Accept the DSN a managed provider hands you, unmodified.

        Belt and braces: the Railway variable is written with the `+asyncpg`
        scheme already, so this normally changes nothing. It exists so that
        pointing `DATABASE_URL` at a reference variable, a connection string
        copied out of a dashboard, or a different provider entirely is a
        working configuration rather than an `InvalidRequestError` about an
        unknown dialect.
        """
        return normalise_async_dsn(v)

    @field_validator("bootstrap_admin_telegram_id", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        """An empty value means "not configured", not "invalid".

        `.env.example` ships this key with nothing after the `=`, which is the
        right way to show somebody a variable they have to fill in. Without
        this, copying the template and filling in only some of it raises a
        pydantic ValidationError at import — a stack trace instead of the
        sentence `check_startup` was written to print.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        """Reject a zone the tz database has never heard of, at load time.

        Otherwise a typo surfaces as ZoneInfoNotFoundError from somewhere deep
        in a handler, hours later, on whichever request first needed a local
        date. Still worth having now that this is only a process default: the
        headless scripts in tools/ read it, and they fail in the same way.
        """
        if not v:
            return v
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"TZ={v!r} is not an IANA time zone name. Use e.g. "
                "'Europe/Istanbul' or 'America/New_York', not an abbreviation."
            ) from exc
        return v

    # Eight, not twelve. The real control is the five-attempt lockout in
    # handlers/gate.py — nothing survives an online guessing attack that a
    # length rule would have stopped and five permanent strikes would not. What
    # this floor still catches is the genuinely careless: "1234", "test", an
    # accidentally truncated paste. Judging whether a phrase is guessable by
    # somebody who knows you is not something a character count can do, and
    # pretending otherwise was the weaker part of the original rule.
    MIN_INVITE_PHRASE_LEN: ClassVar[int] = 8

    @field_validator("invite_phrase")
    @classmethod
    def _long_enough_to_be_secret(cls, v: str) -> str:
        """A short phrase is not a lock.

        The bot is discoverable by anyone who guesses the username, this phrase
        is the only thing between a stranger and someone else's health data, and
        five wrong guesses is all a person gets — so the phrase has to be worth
        more than five guesses. Empty is allowed here and refused by
        `check_startup`, which is where the deployment-shaped failures live.
        """
        phrase = v.strip()
        if phrase and len(phrase) < cls.MIN_INVITE_PHRASE_LEN:
            raise ValueError(
                f"UMAI_INVITE_CODE must be at least {cls.MIN_INVITE_PHRASE_LEN} "
                "characters. Use a few unrelated words, not a password."
            )
        return v

    @property
    def use_webhook(self) -> bool:
        return self.env == "prod"

    def check_startup(self) -> list[str]:
        """Problems worth refusing to start over. Returns human-readable strings."""
        problems: list[str] = []
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN is unset")
        if not self.invite_phrase.strip():
            problems.append(
                "UMAI_INVITE_CODE is unset: the invite phrase is the only thing "
                "between a stranger and someone else's health data"
            )
        if self.bootstrap_admin_telegram_id is None:
            problems.append(
                "UMAI_BOOTSTRAP_ADMIN_TELEGRAM_ID is unset: nobody can be admitted, "
                "because there is no admin to do it"
            )
        if not self.openrouter_api_key:
            problems.append("OPENROUTER_API_KEY is unset: photo logging will fail")
        # TZ is no longer refused. It seeded every user row and set the hour
        # the summary fired, and both of those are per-user columns now; what
        # is left is a process default that nothing load-bearing reads.
        if self.env == "prod":
            if not self.telegram_webhook_url:
                problems.append("prod needs TELEGRAM_WEBHOOK_URL")
            if not self.telegram_webhook_secret:
                problems.append("prod needs TELEGRAM_WEBHOOK_SECRET")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
