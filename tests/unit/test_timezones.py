"""City to zone, and the tz database's legacy names staying out of it."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, available_timezones

import pytest

from umai.core import timezones


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("istanbul", "Europe/Istanbul"),
        ("Istanbul", "Europe/Istanbul"),
        ("  ISTANBUL  ", "Europe/Istanbul"),
        ("new york", "America/New_York"),
        ("new_york", "America/New_York"),
        ("sao paulo", "America/Sao_Paulo"),
        ("nyc", "America/New_York"),
        ("izmir", "Europe/Istanbul"),
        ("Europe/Istanbul", "Europe/Istanbul"),
        ("buenos aires", "America/Argentina/Buenos_Aires"),
    ],
)
def test_resolves_to_one_zone(typed, expected):
    assert timezones.resolve_city(typed) == [expected]


@pytest.mark.parametrize("typed", ["", "   ", "xyzzy", "not a place"])
def test_unknown_input_resolves_to_nothing(typed):
    assert timezones.resolve_city(typed) == []


def test_every_alias_names_a_real_zone():
    """The alias table is hand-written, so a rename in the tz database has to
    surface here rather than as a wizard that cannot resolve a city it claims
    to know."""
    known = available_timezones()
    assert [v for v in timezones.ALIASES.values() if v not in known] == []


def test_aliases_are_normalised_keys():
    assert all(k == timezones.normalise(k) for k in timezones.ALIASES)


def test_legacy_top_level_names_are_not_offered_by_city():
    """`US/Central` and `Canada/Central` both end in "Central". Offering both
    asks a person to choose between two spellings of one place.

    Note the filter is on the top-level name, not on legacy-ness in general:
    `Australia/West` is also a link (to `Australia/Perth`) and does survive.
    That is deliberate — it sits under a real region, it is harmless because
    "west" is not a city anybody types, and a denylist of every link in the tz
    database would need maintaining forever to fix nothing.
    """
    assert timezones.resolve_city("central") == []
    assert timezones.resolve_city("eastern") == []
    # `GMT` and `UTC` are single-segment zone names, so they still resolve —
    # through the exact-name path, which is the escape hatch working, not the
    # city index leaking.
    assert timezones.resolve_city("gmt") == ["GMT"]


def test_a_legacy_name_typed_verbatim_still_works():
    """Not offered, but not refused: someone who types US/Eastern knows what
    they mean."""
    assert timezones.resolve_city("US/Eastern") == ["US/Eastern"]


def _behaviour(zone: str) -> tuple:
    z = ZoneInfo(zone)
    probes = [dt.datetime(y, m, 15) for y in (2020, 2024, 2026, 2030) for m in (1, 4, 7, 10)]
    return tuple((p.replace(tzinfo=z).utcoffset(), p.replace(tzinfo=z).tzname()) for p in probes)


def test_every_collapsed_collision_is_a_link_not_a_choice():
    """The guard on `_preferred`.

    Picking one of several zones for a city is only safe while the alternatives
    are links to the same place. If the tz database ever grows a genuine
    same-city conflict under two geographic regions, this fails here rather
    than in a wizard that silently picks a side.
    """
    grouped: dict[str, list[str]] = {}
    for zone in available_timezones():
        if not zone.startswith(timezones.REGIONS):
            continue
        grouped.setdefault(timezones.normalise(zone.rsplit("/", 1)[-1]), []).append(zone)

    genuine = {
        city: zones
        for city, zones in grouped.items()
        if len(zones) > 1 and len({_behaviour(z) for z in zones}) > 1
    }
    assert genuine == {}
