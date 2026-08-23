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


def test_startup_refuses_without_a_bootstrap_admin():
    """Nobody to admit anybody. A fresh deployment with no admin has no way to
    ever gain one, because admitting is what an admin is for."""
    problems = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        OPENROUTER_API_KEY="x",
        UMAI_INVITE_CODE="correct horse battery staple",
    ).check_startup()
    assert any("BOOTSTRAP_ADMIN" in p for p in problems)


def test_a_short_invite_phrase_is_rejected_at_load():
    """Five wrong guesses is all anyone gets, so the phrase has to be worth
    more than five guesses."""
    with pytest.raises(ValidationError, match="at least"):
        settings(UMAI_INVITE_CODE="hunter2")


def test_a_healthy_configuration_has_no_problems():
    assert settings().check_startup() == []
