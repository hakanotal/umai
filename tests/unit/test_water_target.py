"""Water target display tests.

Verifies that format_day() shows water progress when a target is set, and
that the water_target() helper resolves user vs. system defaults correctly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from umai.core.tools import DayTotals, format_day, water_target


def _user(**kw):
    return SimpleNamespace(
        id=uuid.uuid4(),
        water_target_ml=kw.pop("water_target_ml", None),
    )


def _settings(**kw):
    return SimpleNamespace(water_target_ml=kw.pop("water_target_ml", 2500.0))


def _totals(**kw):
    defaults = dict(
        kcal=0,
        protein_g=0,
        carbs_g=0,
        fat_g=0,
        water_ml=0,
        entry_count=0,
        unmatched_items=0,
    )
    defaults.update(kw)
    return DayTotals(**defaults)


# ---------------------------------------------------------------------------
# water_target() helper
# ---------------------------------------------------------------------------


def test_water_target_user_override():
    u = _user(water_target_ml=3000)
    s = _settings()
    assert water_target(u, s) == 3000


def test_water_target_system_default_when_none():
    u = _user(water_target_ml=None)
    s = _settings(water_target_ml=2000)
    assert water_target(u, s) == 2000


def test_water_target_system_default_when_zero():
    u = _user(water_target_ml=0)
    s = _settings(water_target_ml=2500)
    assert water_target(u, s) == 2500


# ---------------------------------------------------------------------------
# format_day() water display
# ---------------------------------------------------------------------------


def test_format_day_no_water_no_target():
    t = _totals()
    text = format_day(_user(), t, None)
    assert "Water" not in text


def test_format_day_water_no_target():
    t = _totals(water_ml=1200)
    text = format_day(_user(), t, None)
    assert "Water 1200 ml" in text
    assert "/" not in text.split("Water")[1].split("\n")[0]


def test_format_day_water_with_target():
    u = _user(water_target_ml=2500)
    t = _totals(water_ml=1200)
    text = format_day(u, t, None, water_target_ml=2500)
    assert "Water 1200 / 2500 ml [48%]" in text


def test_format_day_water_target_reached():
    u = _user(water_target_ml=2000)
    t = _totals(water_ml=2000)
    text = format_day(u, t, None, water_target_ml=2000)
    assert "Water 2000 / 2000 ml [100%]" in text


def test_format_day_water_over_target():
    u = _user(water_target_ml=2000)
    t = _totals(water_ml=2500)
    text = format_day(u, t, None, water_target_ml=2000)
    assert "Water 2500 / 2000 ml [100%]" in text


def test_format_day_zero_water_with_target():
    u = _user(water_target_ml=2500)
    t = _totals(water_ml=0)
    text = format_day(u, t, None, water_target_ml=2500)
    assert "Water 0 / 2500 ml [0%]" in text


def test_format_day_water_target_none_shows_plain():
    u = _user(water_target_ml=None)
    t = _totals(water_ml=500)
    text = format_day(u, t, None)
    assert "Water 500 ml" in text
    assert "/ " not in text.split("Water")[1].split("\n")[0]
