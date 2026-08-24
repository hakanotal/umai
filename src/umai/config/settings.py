"""Application settings, env-driven, via pydantic-settings.

Mirrors .env.example. The only two values that genuinely differ between the Mac
and the Pi are the Telegram mode and the database URL; every divergence beyond
those is a bug that only appears after deploy.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # The phrase a stranger has to send before the bot will talk to them. This
    # replaced an allowlist of numeric Telegram ids, which meant admitting
    # somebody was an edit to .env and a restart.
    #
    # Never stored: it lives here, `tokens.phrase_matches` compares against it,
    # and no row holds a copy. Rotating it is an edit and a restart, which is
    # the trade for not having a table of codes to manage — and it locks out
    # nobody who is already through, because admission is recorded on the row.
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
    http_port: int = Field(default=8000, alias="UMAI_HTTP_PORT")

    # --- Locale ------------------------------------------------------------
    # Demoted, deliberately. This used to seed every new user row's timezone,
    # which is exactly the "plausible-looking wrong default" the person fields
    # below refused to have — silently giving a stranger the operator's city
    # and therefore the operator's idea of when their day starts. The zone is a
    # per-user column now, asked for by the onboarding wizard.
    #
    # What is left is a process-level default: log timestamps, and the headless
    # scripts in tools/ that have no user to ask. Nothing seeds from it, and
    # the scheduler no longer reads it at all, because an interval trigger has
    # no zone.
    tz: str = Field(default="", alias="TZ")

    # --- Defaults for a new person -----------------------------------------
    # Genuinely a system-wide default rather than somebody's data: everyone
    # starts at 2500 ml and can change it in chat. Contrast the fields that
    # used to live here — sex, height, birth date, goal rate, starting weight,
    # cuisines — which were one person's body and are now columns on `users`,
    # filled in by the wizard.
    water_target_ml: float = Field(default=2500.0, alias="UMAI_WATER_TARGET_ML")

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

    MIN_INVITE_PHRASE_LEN: ClassVar[int] = 12

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
