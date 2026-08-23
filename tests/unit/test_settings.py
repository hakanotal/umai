"""Configuration that is wrong in a way you cannot see.

Timezone is the case this file exists for. Every local-day boundary in Umai —
food totals, the daily summary, step bucketing — is computed from a zone name,
and a wrong one does not raise: it silently moves the start of your day. The
first live deployment ran the container on Europe/Istanbul while the user and
their data were on America/New_York, because a compose `environment:` entry
with a hardcoded fallback quietly overrode the env file that said otherwise.

So: no plausible-looking default, and an unknown zone name fails at load rather
than hours later inside a request.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from umai.config.settings import Settings


def settings(**kw) -> Settings:
    """A Settings with the unrelated required fields filled in."""
    base = {
        "TELEGRAM_BOT_TOKEN": "x",
        "TELEGRAM_ALLOWED_USER_IDS": "1",
        "OPENROUTER_API_KEY": "x",
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
    """The regression guard. A hardcoded default here seeds every user row and
    every day boundary, and looks entirely plausible while being wrong."""
    assert settings(TZ="").tz == ""


def test_startup_refuses_without_a_timezone():
    problems = settings(TZ="").check_startup()
    assert any("TZ is unset" in p for p in problems)


def test_startup_is_happy_with_one():
    assert not [p for p in settings().check_startup() if "TZ" in p]
