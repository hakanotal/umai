"""Configuration that is wrong in a way you cannot see.

Timezone is the case this file exists for. Every local-day boundary in Umai —
food totals, the daily summary, step bucketing — is computed from a zone name,
and a wrong one does not raise: it silently moves the start of your day. The
first live deployment ran the container on Europe/Istanbul while the user and
their data were on America/New_York, because a compose `environment:` entry
with a hardcoded fallback quietly overrode the env file that said otherwise.

So: no plausible-looking default, and an unknown zone name fails at load rather
than hours later inside a request.

Since multi-user, TZ is a *process* default and nothing load-bearing reads it —
every local day is computed from `users.tz`, which the onboarding wizard fills
in per person. The validator stays because the headless scripts in tools/ still
read the setting and would fail the same way; the startup refusal is gone,
because refusing to boot over a value nothing depends on is noise.

The access settings are here for the same reason the zone is: UMAI_INVITE_CODE
being empty is not visible from the outside, and it means the bot admits
anybody who sends it a blank line.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from umai.config.settings import Settings


def settings(**kw) -> Settings:
    """A Settings with the unrelated required fields filled in."""
    base = {
        "TELEGRAM_BOT_TOKEN": "x",
        "OPENROUTER_API_KEY": "x",
        "UMAI_INVITE_CODE": "correct horse battery staple",
        "UMAI_BOOTSTRAP_ADMIN_TELEGRAM_ID": "1",
        "TZ": "Europe/Istanbul",
    }
    return Settings(_env_file=None, **(base | kw))


def test_a_real_zone_is_accepted():
    assert settings(TZ="America/New_York").tz == "America/New_York"


def test_an_unknown_zone_is_rejected_at_load_not_at_use():
    """A typo must not survive to become a ZoneInfoNotFoundError inside a
    handler on whichever request first needed a local date."""
    with pytest.raises(ValidationError, match="not an IANA time zone"):
        settings(TZ="Europe/Istanbulll")


def test_an_abbreviation_is_not_a_zone():
    """EST is an offset, not a zone: it has no DST rules. The distinction is
    invisible until the clocks change."""
    with pytest.raises(ValidationError, match="not an IANA time zone"):
        settings(TZ="EST5EDT-ish")


def test_there_is_no_default_city():
    """The regression guard. A hardcoded default here used to seed every user
    row and every day boundary, and looked entirely plausible while being
    wrong."""
    assert settings(TZ="").tz == ""


def test_startup_no_longer_refuses_without_a_timezone():
    """It did, and should not now.

    TZ seeded new user rows and decided when the summary fired. Both are
    per-user columns since multi-user, and the scheduler stopped reading a zone
    at all when it became an interval tick — so refusing to boot over this
    would be refusing over a value nothing depends on.
    """
    assert not [p for p in settings(TZ="").check_startup() if "TZ" in p]


# --- access ----------------------------------------------------------------


def test_startup_refuses_without_an_invite_phrase():
    """An empty phrase is not a lock. `phrase_matches` refuses an empty
    expected value as well; this is the outer of the two."""
    problems = settings(UMAI_INVITE_CODE="").check_startup()
    assert any("UMAI_INVITE_CODE" in p for p in problems)


def test_startup_refuses_without_a_bootstrap_admin(monkeypatch):
    """Nobody to admit anybody. A fresh deployment with no admin has no way to
    ever gain one, because admitting is what an admin is for.

    The env var is cleared explicitly because `_env_file=None` only stops
    pydantic reading the *files* — os.environ is still a source, and `just`
    loads .local.env into it before pytest starts. Without this the test read
    the developer's own admin id and concluded the setting was configured."""
    monkeypatch.delenv("UMAI_BOOTSTRAP_ADMIN_TELEGRAM_ID", raising=False)
    problems = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        OPENROUTER_API_KEY="x",
        UMAI_INVITE_CODE="correct horse battery staple",
    ).check_startup()
    assert any("BOOTSTRAP_ADMIN" in p for p in problems)


def test_a_careless_invite_phrase_is_rejected_at_load():
    """The floor catches the obviously careless — a truncated paste, "1234" —
    and nothing more. Guessability by somebody who knows you is not something a
    character count can judge; the five-attempt lockout is the real control."""
    with pytest.raises(ValidationError, match="at least"):
        settings(UMAI_INVITE_CODE="1234")


def test_a_short_but_deliberate_phrase_is_accepted():
    assert settings(UMAI_INVITE_CODE="Albany2026").invite_phrase == "Albany2026"


def test_a_healthy_configuration_has_no_problems():
    assert settings().check_startup() == []


def test_a_blank_bootstrap_id_is_unset_rather_than_invalid():
    """`.env.example` ships this key with nothing after the `=`, which is how
    you show somebody a value they must supply. Parsing that as an integer
    raises at import, so copying the template and filling in only half of it
    gave a stack trace instead of the sentence check_startup exists to print."""
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        OPENROUTER_API_KEY="x",
        UMAI_INVITE_CODE="correct horse battery staple",
        UMAI_BOOTSTRAP_ADMIN_TELEGRAM_ID="",
    )
    assert s.bootstrap_admin_telegram_id is None
    assert any("BOOTSTRAP_ADMIN" in p for p in s.check_startup())


def test_the_whole_template_loads_and_says_what_is_missing():
    """The first-run path, end to end: someone copies .env.example, fills in
    nothing, and starts the bot. Every problem should be a sentence."""
    import re
    from pathlib import Path

    template = Path(__file__).resolve().parents[2] / ".env.example"
    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", template.read_text(), re.M))
    values = {k: v.split("#")[0].strip() for k, v in values.items()}

    problems = Settings(_env_file=None, **values).check_startup()
    assert {"TELEGRAM_BOT_TOKEN", "UMAI_INVITE_CODE", "OPENROUTER_API_KEY"} <= {
        w for p in problems for w in p.split() if w.isupper()
    }


# --- the DSN a managed provider actually hands you --------------------------
#
# Railway publishes `postgresql://...`, which SQLAlchemy reads as the psycopg2
# dialect and then fails to import, and it spells connection options the way
# libpq does, which asyncpg rejects as unknown keywords. Neither failure is
# visible until something opens a connection — in the deployed case, inside the
# pre-deploy migration step, where the error is a stack trace in a build log.


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # the two schemes a provider hands out
        ("postgresql://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        ("postgres://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        # already correct: left exactly alone
        ("postgresql+asyncpg://u:p@h:5432/d", "postgresql+asyncpg://u:p@h:5432/d"),
        # libpq-only options, alone and alongside one asyncpg understands
        ("postgresql://u:p@h/d?sslmode=require", "postgresql+asyncpg://u:p@h/d"),
        (
            "postgresql://u:p@h/d?sslmode=require&application_name=umai",
            "postgresql+asyncpg://u:p@h/d?application_name=umai",
        ),
        # empty stays empty rather than becoming a scheme with nothing after it
        ("", ""),
    ],
)
def test_dsn_normalisation(given, expected):
    from umai.config.settings import normalise_async_dsn

    assert normalise_async_dsn(given) == expected


def test_a_password_with_url_syntax_in_it_survives():
    """The reason this is string surgery and not a urllib round-trip.

    Generated passwords contain `/`, `?` and `%` often enough that reassembling
    the URL through a parser is a way to corrupt a credential, and the symptom
    would be an authentication failure nobody traces back to here."""
    from umai.config.settings import normalise_async_dsn

    dsn = "postgresql://umai:pa%2Fss%3Fword@h:5432/d"
    assert normalise_async_dsn(dsn) == "postgresql+asyncpg://umai:pa%2Fss%3Fword@h:5432/d"


def test_the_setting_normalises_on_load():
    assert settings(DATABASE_URL="postgresql://u:p@h/d").database_url == (
        "postgresql+asyncpg://u:p@h/d"
    )


def test_railway_port_is_the_fallback_and_never_the_override():
    """Railway assigns the port; nobody should have to copy it into a second
    variable. An explicit UMAI_HTTP_PORT still wins, so running locally
    somewhere that also sets PORT does not move the bind."""
    assert settings(PORT="4321").http_port == 4321
    assert settings(PORT="4321", UMAI_HTTP_PORT="8000").http_port == 8000
    assert settings().http_port == 8000
