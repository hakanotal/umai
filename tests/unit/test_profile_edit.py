"""The profile editor's contract with the wizard it reuses.

The editor's value is that it does not restate what the wizard knows: prompts,
parsers and display formats all come from `core.onboarding`, and the list of
editable fields is derived from `STEPS`. These tests hold that derivation in
place, because the failure it prevents is silent — a question added to the
wizard and forgotten in the editor is not a crash, it is a field nobody can
ever change again.

Handler behaviour that needs a database lives in
`tests/integration/test_profile_edit_flow.py`.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from umai.core import cuisines as cuisines_mod
from umai.core import onboarding as ob
from umai.telegram import keyboards


def _user(**overrides):
    row = {
        "tz": "Europe/Istanbul",
        "sex": "female",
        "height_cm": 168.0,
        "birth_date": dt.date(1992, 3, 14),
        "onboarding_weight_kg": 62.0,
        "goal_rate_kg_per_week": -0.25,
        "cuisines": ["turkish"],
    }
    row.update(overrides)
    return SimpleNamespace(**row)


# --- which fields are offered ----------------------------------------------


def test_every_wizard_step_is_editable_except_the_declared_exclusions():
    """The derivation itself.

    A new `Step` becomes editable for free; making one non-editable is a
    deliberate entry in `NOT_EDITABLE` rather than an omission.
    """
    editable = {s.field for s in ob.editable_steps()}
    assert editable == {s.field for s in ob.STEPS} - ob.NOT_EDITABLE


def test_starting_weight_is_not_editable():
    """Weight is a LogEntry; `onboarding_weight_kg` only records what was said.

    Offering it here would edit a historical statement and change nothing the
    bot reads — the trend, the target and the calibration fit all come from the
    log — while looking exactly like the control that does.
    """
    assert ob.WEIGHT_FIELD in ob.NOT_EDITABLE
    step = ob.step_for(ob.WEIGHT_FIELD)
    assert step is not None
    assert step.field in ob.NOT_EDITABLE


def test_editable_steps_keep_wizard_order():
    order = [s.field for s in ob.STEPS if s.field not in ob.NOT_EDITABLE]
    assert [s.field for s in ob.editable_steps()] == order


def test_every_step_has_a_button_label_distinct_from_its_prompt():
    """A prompt is a sentence; a button needs a noun. Neither may be missing."""
    for step in ob.STEPS:
        assert step.label and len(step.label) <= 20, step.field
        assert step.label != step.prompt


@pytest.mark.parametrize("field", ["", "status", "is_admin", "health_token", "id"])
def test_step_for_refuses_anything_not_an_editable_step(field):
    """`step_for` takes a field name in from Telegram callback data.

    Non-editable and non-existent have to be the same answer, or the editor
    becomes a way to write arbitrary columns on the user row.
    """
    assert ob.step_for(field) is None


# --- describe is the inverse of parse ---------------------------------------


@pytest.mark.parametrize(
    ("field", "raw", "expected"),
    [
        ("height_cm", "1.80", "180 cm"),  # metres are converted on the way in
        ("height_cm", "180", "180 cm"),
        ("sex", "erkek", "Male"),
        ("birth_date", "14.03.1992", "1992-03-14"),
        ("goal_rate_kg_per_week", "-0.5", "Lose (0.5 kg/wk)"),
        ("goal_rate_kg_per_week", "0", "Maintain"),
    ],
)
def test_describe_reads_back_what_parse_wrote(field, raw, expected):
    """The round trip. A height typed as "1.80" is stored 180.0 and has to read
    back as "180 cm" — not "180.0", and not "1.8"."""
    step = ob.step_for(field)
    result = step.parse(raw)
    assert isinstance(result, ob.Ok), result
    assert ob.describe(step, result.value) == expected


def test_describe_shows_a_goal_rate_no_button_offers():
    """A rate written before the choices changed is shown, not hidden.

    It is still what the target is computed from, so a menu that rendered it as
    "not set" would be lying about a live number.
    """
    step = ob.step_for("goal_rate_kg_per_week")
    assert ob.describe(step, -0.9) == "-0.90 kg/wk"


def test_describe_names_cuisines_rather_than_printing_slugs():
    step = ob.step_for("cuisines")
    described = ob.describe(step, ["turkish"])
    assert "Turkish" in described
    assert described != "turkish"  # the slug itself is not a label


def test_describe_caps_a_long_cuisine_list_with_an_honest_count():
    """Six cuisines with flag emoji do not fit on a button, and Telegram
    ellipsises silently. "+3" says how much was left out."""
    step = ob.step_for("cuisines")
    slugs = list(cuisines_mod.CUISINES)[:6]
    described = ob.describe(step, slugs)
    assert described.endswith("+3")
    assert described.count(",") == 2


@pytest.mark.parametrize("field", ["tz", "sex", "height_cm", "birth_date", "cuisines"])
def test_unset_fields_read_as_not_set(field):
    """Both spellings of empty — None, and the empty list cuisines uses."""
    step = ob.step_for(field)
    empty = [] if field == "cuisines" else None
    assert ob.describe(step, empty) == "not set"


# --- the keyboard -----------------------------------------------------------


def _rows(user):
    return [
        (s.field, s.label, ob.describe(s, getattr(user, s.field, None)))
        for s in ob.editable_steps()
    ]


def test_profile_menu_shows_a_button_per_editable_field_plus_back():
    markup = keyboards.profile_menu(_rows(_user()))
    assert len(markup.inline_keyboard) == len(ob.editable_steps()) + 1
    assert markup.inline_keyboard[-1][0].callback_data == "cfg:configure"


def test_profile_menu_buttons_carry_the_value_so_a_check_costs_no_taps():
    markup = keyboards.profile_menu(_rows(_user()))
    labels = [row[0].text for row in markup.inline_keyboard]
    assert any("Height · 168 cm" in label for label in labels)
    assert any("Gender · Female" in label for label in labels)


def test_profile_callback_data_is_within_telegrams_64_byte_cap():
    """The cap is on the encoded bytes, and the longest field name here is
    `goal_rate_kg_per_week`. A field long enough to overflow would fail only
    when tapped."""
    markup = keyboards.profile_menu(_rows(_user()))
    for row in markup.inline_keyboard:
        assert len(row[0].callback_data.encode()) <= 64, row[0].callback_data


def test_profile_prefixes_do_not_collide_with_the_wizards():
    """The wizard's router is behind `NeedsGate()` and this one behind
    `IsActive()`, so a shared prefix would be answered for exactly one of the
    two callers and silently dropped for the other."""
    profile = {
        keyboards.profile_menu(_rows(_user())).inline_keyboard[0][0].callback_data,
        keyboards.profile_sex().inline_keyboard[0][0].callback_data,
        keyboards.profile_goal(ob.GOAL_CHOICES).inline_keyboard[0][0].callback_data,
        keyboards.profile_confirm_tz("Europe/Istanbul").inline_keyboard[0][0].callback_data,
        keyboards.profile_timezones(["Europe/Istanbul"]).inline_keyboard[0][0].callback_data,
    }
    wizard = {
        keyboards.onboarding_sex().inline_keyboard[0][0].callback_data,
        keyboards.onboarding_goal(ob.GOAL_CHOICES).inline_keyboard[0][0].callback_data,
        keyboards.onboarding_confirm_tz("Europe/Istanbul").inline_keyboard[0][0].callback_data,
        keyboards.onboarding_timezones(["Europe/Istanbul"]).inline_keyboard[0][0].callback_data,
    }
    assert profile & wizard == set()
    assert all(d.startswith("prof:") for d in profile)


def test_the_field_prefix_cannot_be_confused_with_the_value_prefixes():
    """`prof:field:` opens a question, `prof:set:` answers one, `prof:tz*`
    confirms a zone. The handlers are chosen by prefix, so an overlap would
    route a tap to the wrong one."""
    field_cb = keyboards.profile_menu(_rows(_user())).inline_keyboard[0][0].callback_data
    assert field_cb.startswith("prof:field:")
    assert not field_cb.startswith(("prof:set:", "prof:tz"))
    assert keyboards.profile_sex().inline_keyboard[0][0].callback_data.startswith("prof:set:")


def test_the_editor_offers_the_same_timezone_regions_as_the_wizard():
    """Same cities, same layout, different prefix.

    Someone who onboarded by tapping "Istanbul" meets that button again rather
    than a bare instruction to type a city.
    """
    wizard = keyboards.onboarding_tz_regions().inline_keyboard
    editor = keyboards.profile_tz_regions().inline_keyboard
    assert [[b.text for b in row] for row in wizard] == [[b.text for b in row] for row in editor]


def test_the_editors_region_buttons_are_answered_by_the_editors_own_router():
    """A region keyboard drawn with the wizard's `ob:` prefix would render for
    a settled user and do nothing when tapped — the wizard's router is behind
    `NeedsGate()`, which an active user fails."""
    for row in keyboards.profile_tz_regions().inline_keyboard:
        for button in row:
            assert button.callback_data.startswith("prof:")


def test_new_york_is_on_the_region_keyboard():
    """It was named in the commit that added the keyboard and absent from it,
    which left the Americas represented by Los Angeles alone."""
    labels = [b.text for row in keyboards.onboarding_tz_regions().inline_keyboard for b in row]
    assert "New York" in labels


def test_configure_menu_offers_the_profile():
    """The entry point the whole feature hangs off."""
    data = [b.callback_data for row in keyboards.configure_menu().inline_keyboard for b in row]
    assert "cfg:profile" in data
