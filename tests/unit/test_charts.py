"""The digest chart. Rendering is checked by rendering; the arithmetic that
decides bar heights is checked directly, because that is where a wrong picture
would come from.

The one property worth a test rather than an eyeball: a missing step reading
and a zero step reading must not draw the same thing. Both are "no bar" under
naive plotting, and they mean opposite things — see `tools.daily_steps`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from umai.analytics import charts

DAYS = [dt.date(2026, 8, 18) + dt.timedelta(days=i) for i in range(7)]
KCAL = [2180.0, 1950.0, 2460.0, 2020.0, 0.0, 2310.0, 1740.0]
WATER = [2100.0, 2600.0, 1400.0, 2500.0, 800.0, 2500.0, 1900.0]
STEPS: list[int | None] = [7400, 9100, 12500, None, 3200, 8050, 0]


def _render(**kw) -> bytes:
    args = {
        "kcal_target": 2150.0,
        "water_target_ml": 2500.0,
    } | kw
    return charts.week_overview(DAYS, KCAL, WATER, STEPS, **args)


def test_week_overview_renders_a_png():
    png = _render()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 5_000


def test_week_overview_survives_no_calorie_target():
    """A user with no weigh-in yet has no target, and still gets a chart."""
    assert _render(kcal_target=None).startswith(b"\x89PNG\r\n\x1a\n")


def test_week_overview_survives_an_entirely_empty_week():
    png = charts.week_overview(
        DAYS,
        [0.0] * 7,
        [0.0] * 7,
        [None] * 7,
        kcal_target=None,
        water_target_ml=2500.0,
    )
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize(
    ("value", "goal", "expected"),
    [
        (2150.0, 2150.0, 100.0),
        (1075.0, 2150.0, 50.0),
        (0.0, 2150.0, 0.0),
    ],
)
def test_pct_scales_against_the_goal(value, goal, expected):
    assert charts._pct(value, goal) == pytest.approx(expected)


def test_pct_keeps_a_missing_reading_missing():
    """None must not become zero. A day the phone failed to sync is not a day
    spent sitting down, and the chart draws a '?' rather than a bar."""
    assert charts._pct(None, 8000) is None


def test_pct_refuses_to_divide_by_an_absent_goal():
    assert charts._pct(2150.0, None) is None
    assert charts._pct(2150.0, 0.0) is None


def test_a_real_zero_still_draws_something():
    """A logged zero gets a visible stub, so it cannot be mistaken for the
    absent bar that a missing reading leaves behind."""
    assert charts._ZERO_STUB > 0
