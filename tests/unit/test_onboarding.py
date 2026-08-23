"""The wizard's brains: the step derivation and every parser.

No Telegram, no database. `Fake` stands in for the `User` row, which is the
whole point of `next_step` taking anything with the right attributes.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from umai.core import onboarding as ob


def _blank(**kw):
    fields = {step.field: None for step in ob.STEPS}
    fields["cuisines"] = []
    return SimpleNamespace(**{**fields, **kw})


# --- the state machine that isn't one ---------------------------------------


def test_a_blank_user_is_asked_the_timezone_first():
    """Order is load-bearing: every later local day depends on the zone, and a
    wrong zone is the only answer here that is invisible when wrong."""
    assert ob.next_step(_blank()).field == "tz"


def test_each_answered_field_advances_the_wizard():
    state = _blank()
    seen = []
    for _ in ob.STEPS:
        current = ob.next_step(state)
        assert current is not None
        seen.append(current.field)
        setattr(state, current.field, ["turkish"] if current.field == "cuisines" else 1)
    assert seen == [s.field for s in ob.STEPS]
    assert ob.next_step(state) is None


def test_an_empty_cuisine_list_counts_as_unanswered():
    """The column has a server default of `{}`, so treating None as the only
    unset value would skip the last question for everybody."""
    state = _blank(tz="Europe/Istanbul", sex="male", height_cm=180.0)
    state.birth_date = dt.date(1990, 5, 1)
    state.onboarding_weight_kg = 88.0
    state.goal_rate_kg_per_week = -0.5
    assert ob.next_step(state).field == "cuisines"
    state.cuisines = ["turkish"]
    assert ob.next_step(state) is None


def test_a_zero_goal_rate_counts_as_answered():
    """Maintain is 0.0, and 0.0 is falsy. A truthiness check here would loop
    anyone choosing maintain back onto the same question forever."""
    state = _blank(tz="Europe/Istanbul", sex="male", height_cm=180.0)
    state.birth_date = dt.date(1990, 5, 1)
    state.onboarding_weight_kg = 88.0
    state.goal_rate_kg_per_week = 0.0
    assert ob.next_step(state).field == "cuisines"


def test_progress_counts_from_one():
    assert ob.progress(_blank()) == (1, len(ob.STEPS))


# --- parsers ----------------------------------------------------------------


def test_timezone_accepts_a_city_or_a_zone():
    assert ob.parse_tz("istanbul") == ob.Ok("Europe/Istanbul")
    assert ob.parse_tz("Europe/Istanbul") == ob.Ok("Europe/Istanbul")


def test_an_unknown_city_is_a_retry_with_examples():
    result = ob.parse_tz("xyzzy")
    assert isinstance(result, ob.Retry)
    assert "Europe/Istanbul" in result.message


@pytest.mark.parametrize("typed", ["male", "Male", "M", "erkek"])
def test_sex_accepts_the_usual_spellings(typed):
    assert ob.parse_sex(typed) == ob.Ok("male")


@pytest.mark.parametrize(
    ("typed", "expected"),
    [("180", 180.0), ("180cm", 180.0), ("1.80", 180.0), ("1,80", 180.0)],
)
def test_height_accepts_centimetres_metres_and_a_comma_decimal(typed, expected):
    """A comma is what a Turkish or German keyboard produces, and it is what
    every naive float() call rejects."""
    assert ob.parse_height(typed) == ob.Ok(expected)


@pytest.mark.parametrize("typed", ["", "tall", "300", "40"])
def test_implausible_heights_are_refused(typed):
    assert isinstance(ob.parse_height(typed), ob.Retry)


@pytest.mark.parametrize("typed", ["1990-05-01", "01.05.1990", "01/05/1990"])
def test_birth_date_accepts_iso_and_day_first(typed):
    assert ob.parse_birth_date(typed) == ob.Ok(dt.date(1990, 5, 1))


def test_an_ambiguous_date_is_read_day_first_not_month_first():
    """03/04/1990 is March in one convention and April in the other. Only one
    convention is accepted, so the result is wrong-or-refused rather than
    silently wrong: a birth date off by a month shifts every BMR this system
    will ever compute for the person."""
    assert ob.parse_birth_date("03/04/1990") == ob.Ok(dt.date(1990, 4, 3))


@pytest.mark.parametrize("typed", ["yesterday", "1990", "31.02.1990"])
def test_unparseable_dates_are_refused(typed):
    assert isinstance(ob.parse_birth_date(typed), ob.Retry)


def test_weight_is_bounded_by_the_safety_check():
    assert ob.parse_weight("88") == ob.Ok(88.0)
    assert ob.parse_weight("88,5") == ob.Ok(88.5)
    assert isinstance(ob.parse_weight("880"), ob.Retry)
    assert isinstance(ob.parse_weight("heavy"), ob.Retry)


def test_goal_accepts_only_the_offered_rates():
    assert ob.parse_goal("-0.5") == ob.Ok(-0.5)
    assert isinstance(ob.parse_goal("-1.5"), ob.Retry)


def test_goal_types_match_their_rates():
    for _, rate, kind in ob.GOAL_CHOICES:
        assert ob.goal_type_for(rate) == kind


def test_cuisines_are_button_only():
    assert isinstance(ob.parse_cuisines("turkish"), ob.Retry)
