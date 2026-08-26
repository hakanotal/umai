"""Keyboard contract tests.

Telegram caps callback data at 64 bytes, which is the constraint the entry
prefixes exist to satisfy, and the reply keyboard's labels are matched by
exact text in the menu handler, which breaks silently if a label drifts
between the two files. Both are cheap to pin here.
"""

from __future__ import annotations

import datetime as dt
import uuid

from umai.core import onboarding as ob
from umai.core.tools import RecipeListItem, TodayEntry
from umai.db.models import EntryKind
from umai.telegram import keyboards


def _entry(kind: EntryKind, **kw) -> TodayEntry:
    defaults = dict(
        entry_id=uuid.uuid4(),
        kind=kind,
        occurred_at=dt.datetime(2026, 8, 22, 10, 30, tzinfo=dt.UTC),
        kcal=573.0,
        ml=300.0,
        item_count=2,
        local_time="10:30",
    )
    defaults.update(kw)
    return TodayEntry(**defaults)


def _all_callback_data(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_meal_actions_callback_data_fits_telegrams_64_bytes():
    markup = keyboards.meal_actions(str(uuid.uuid4()), 8)
    for data in _all_callback_data(markup):
        assert len(data.encode()) <= 64, data


def test_edit_keyboards_callback_data_fits_telegrams_64_bytes():
    prefix = str(uuid.uuid4())[:8]
    entry = _entry(EntryKind.food)
    for markup in (
        keyboards.edit_list([entry]),
        keyboards.edit_meal(prefix, 8),
        keyboards.edit_water(prefix),
        keyboards.delete_confirm(prefix),
        keyboards.summary_actions(),
    ):
        for data in _all_callback_data(markup):
            assert len(data.encode()) <= 64, data


def test_main_menu_labels_are_all_handled():
    """The reply keyboard sends its label as text; each label must be matched
    by exact text before the intent classifier sees it. Some are in
    MENU_LABELS (menu.py), Library has a text filter on its own router,
    and Configure shows an inline keyboard."""
    from umai.telegram.handlers import MENU_LABELS

    labels = {
        *(b.text for row in keyboards.main_menu().keyboard for b in row),
    }
    # Library is handled by its own router's text filter.
    own_router_labels = {keyboards.BTN_LIBRARY}
    assert labels == MENU_LABELS | own_router_labels


def test_edit_list_labels_meals_and_water_differently():
    meal = _entry(EntryKind.food, ml=0.0)
    water = _entry(EntryKind.water, kcal=0.0, item_count=0)
    markup = keyboards.edit_list([meal, water])
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("kcal" in t for t in labels)
    assert any("ml" in t for t in labels)


def test_today_entry_prefix_is_8_chars_and_button_uses_it():
    entry = _entry(EntryKind.water)
    assert len(entry.prefix) == 8
    markup = keyboards.edit_list([entry])
    data = _all_callback_data(markup)[0]
    assert data == f"edit:{entry.prefix}"


def test_onboarding_keyboards_callback_data_fits_telegrams_64_bytes():
    for markup in (
        keyboards.onboarding_sex(),
        keyboards.onboarding_goal(ob.GOAL_CHOICES),
        keyboards.onboarding_tz_regions(),
        keyboards.onboarding_confirm_tz("Europe/Istanbul"),
    ):
        for data in _all_callback_data(markup):
            assert len(data.encode()) <= 64, data


def test_emoji_vocabulary_is_consistent():
    """One 💧 everywhere, not 💧 here and 🥤 there. This is the closest thing
    to a colour theme a Telegram bot has."""
    assert keyboards.WATER_250.startswith(keyboards.WATER)
    assert keyboards.BTN_TODAY.startswith(keyboards.CALENDAR)
    assert keyboards.BTN_WEEK.startswith(keyboards.CHART)
    assert keyboards.BTN_EDIT.startswith(keyboards.PENCIL)
    assert keyboards.BTN_WEIGH.startswith(keyboards.SCALE)
    assert "Recipes" in keyboards.BTN_LIBRARY
    assert keyboards.BTN_CONFIGURE.startswith(keyboards.GEAR)


def test_library_items_shows_recipes_only_with_kcal():
    """My Recipes shows only saved recipes — the library of foods-logged-often
    was the confusion this replaces — with per-serving kcal in the label and
    grams hidden from the button (the grams stay in the callback data so the
    quick-log handler can reprice)."""
    rec_id = uuid.uuid4()
    markup = keyboards.library_items(
        recipes=[
            RecipeListItem(
                recipe_id=rec_id,
                name="morning yogurt",
                portion_grams=300.0,
                times_logged=2,
                kcal=540.0,
            )
        ],
    )
    data = _all_callback_data(markup)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert data == [f"rec:{rec_id}:300"]
    assert labels == ["morning yogurt — 540 kcal"]
    # No library-food buttons survive the recipes-only rewrite.
    assert not any(d.startswith("lib:") for d in data)


def test_library_items_callback_data_fits_telegrams_64_bytes():
    markup = keyboards.library_items(
        recipes=[
            RecipeListItem(
                recipe_id=uuid.uuid4(),
                name="a very long recipe name that still fits",
                portion_grams=320.0,
                times_logged=2,
                kcal=540.0,
            )
        ],
    )
    for data in _all_callback_data(markup):
        assert len(data.encode()) <= 64, data


def test_meal_actions_has_save_as_recipe_row_above_looks_right():
    """The save-as-recipe action sits in its own row, above Looks right, so the
    whole-meal action never shares a row with a per-item gram fix."""
    entry = str(uuid.uuid4())
    markup = keyboards.meal_actions(entry, 3)
    rows = markup.inline_keyboard
    # The last two rows are Save as recipe, then Looks right — in that order.
    assert rows[-2][0].text == f"{keyboards.SAVE} Save as recipe"
    assert rows[-2][0].callback_data == f"sv:{entry[: keyboards.ENTRY_PREFIX_LEN]}"
    assert rows[-1][0].text == f"{keyboards.CHECK} Looks right"
    assert rows[-1][0].callback_data == f"ok:{entry[: keyboards.ENTRY_PREFIX_LEN]}"


def test_recipe_draft_keyboard_shape():
    """The adjust step: per-item fix buttons, cooked weight, save, cancel."""
    markup = keyboards.recipe_draft(3)
    rows = markup.inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["rfix:1", "rfix:2", "rfix:3"]
    assert rows[-3][0].callback_data == "rcook:"
    assert rows[-2][0].callback_data == "rsave:"
    assert rows[-1][0].callback_data == "rcancel:"


def test_recipe_draft_cooked_weight_label_reflects_the_set_value():
    markup = keyboards.recipe_draft(2, cooked_grams=1100.0)
    rows = markup.inline_keyboard
    assert "1100g" in rows[-3][0].text


def test_recipe_draft_callback_data_fits_telegrams_64_bytes():
    markup = keyboards.recipe_draft(8)
    for data in _all_callback_data(markup):
        assert len(data.encode()) <= 64, data
