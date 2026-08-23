"""Application settings, env-driven, via pydantic-settings.

Mirrors .env.example. The only two values that genuinely differ between the Mac
and the Pi are the Telegram mode and the database URL; every divergence beyond
those is a bug that only appears after deploy.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .local.env first so a local override wins, then the conventional name.
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

    # --- The person --------------------------------------------------------
    # No defaults on purpose. BMR, the safety floors and the initial expenditure
    # estimate are all seeded from these, and a plausible-looking wrong default
    # is worse than a startup failure.
    # No default, deliberately. A hardcoded city here is exactly the
    # "plausible-looking wrong default" the person fields below refuse to have:
    # it silently seeds every user row and every local-day boundary, and it
    # looks right until someone notices their day starts at the wrong hour.
    # check_startup() refuses to boot without it.
    tz: str = Field(default="", alias="TZ")
    sex: Literal["male", "female", ""] = Field(default="", alias="UMAI_SEX")
    height_cm: float | None = Field(default=None, alias="UMAI_HEIGHT_CM")
    birth_date: date | None = Field(default=None, alias="UMAI_BIRTH_DATE")
    goal_rate_kg_per_week: float = Field(default=-0.5, alias="UMAI_GOAL_RATE_KG_PER_WEEK")
    # Seeds the first weight reading at onboarding. The calibration engine
    # depends on the weight series more than anything else, so the series
    # should start from day one rather than from the first manual weigh-in.
    start_weight_kg: float | None = Field(default=None, alias="UMAI_START_WEIGHT_KG")
    # Comma-separated cuisine slugs, seeding a new user's list. What the user
    # eats is the single cheapest piece of context a perception model can be
    # given: "reddish paste on thin flatbread" is a guess, "lahmacun" is an
    # identification, and only one of them resolves against a food table.
    # Editable in chat afterwards with /cuisines, which is the source of truth.
    cuisines: str = Field(default="turkish", alias="UMAI_CUISINES")
    # Daily water intake target in ml. Seeds new users; editable in chat.
    water_target_ml: float = Field(default=2500.0, alias="UMAI_WATER_TARGET_ML")

    @field_validator("tz")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        """Reject a zone the tz database has never heard of, at load time.

        Otherwise a typo surfaces as ZoneInfoNotFoundError from somewhere deep
        in a handler, hours later, on whichever request first needed a local
        date.
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
    def cuisine_list(self) -> list[str]:
        from umai.core.cuisines import normalise

        return normalise((self.cuisines or "").replace(";", ",").split(","))

    @property
    def use_webhook(self) -> bool:
        return self.env == "prod"

    def require_person(self) -> None:
        """Fail loudly rather than guess. Called by anything that computes a target."""
        missing = [
            name
            for name, value in (
                ("UMAI_SEX", self.sex),
                ("UMAI_HEIGHT_CM", self.height_cm),
                ("UMAI_BIRTH_DATE", self.birth_date),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Cannot compute a calorie target without: "
                + ", ".join(missing)
                + ". These seed BMR and the safety floors;"
                + " see docs/umai-project-plan.md section 11."
            )

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
        if not self.tz:
            problems.append(
                "TZ is unset: it decides where every local day starts, seeds new "
                "user rows, and sets the hour the evening summary fires. Guessing "
                "it wrong is invisible until a day lands on the wrong date."
            )
        if self.env == "prod":
            if not self.telegram_webhook_url:
                problems.append("prod needs TELEGRAM_WEBHOOK_URL")
            if not self.telegram_webhook_secret:
                problems.append("prod needs TELEGRAM_WEBHOOK_SECRET")
            if not self.health_ingest_token:
                problems.append("prod needs HEALTH_INGEST_TOKEN: the ingest endpoint is exposed")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
