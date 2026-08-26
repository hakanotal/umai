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
from umai.core.tools import LibraryItem, RecipeListItem, TodayEntry
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


def test_library_items_puts_recipes_above_foods_with_distinct_prefixes():
    """Recipes sit above foods and carry a `rec:` prefix so a recipe id is
    never fed to the food quick-log handler (and vice versa)."""
    rec_id = uuid.uuid4()
    food_id = uuid.uuid4()
    markup = keyboards.library_items(
        items=[LibraryItem(food_id=food_id, name="rice", portion_grams=150.0, times_logged=5)],
        recipes=[
            RecipeListItem(
                recipe_id=rec_id, name="lentil soup", portion_grams=320.0, times_logged=2
            )
        ],
    )
    data = _all_callback_data(markup)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert data == [f"rec:{rec_id}:320", f"lib:{food_id}:150"]
    assert labels[0].startswith("lentil soup")
    assert labels[1].startswith("rice")


def test_library_items_callback_data_fits_telegrams_64_bytes():
    markup = keyboards.library_items(
        items=[LibraryItem(food_id=uuid.uuid4(), name="rice", portion_grams=150.0, times_logged=5)],
        recipes=[
            RecipeListItem(
                recipe_id=uuid.uuid4(),
                name="lentil soup",
                portion_grams=320.0,
                times_logged=2,
            )
        ],
    )
    for data in _all_callback_data(markup):
        assert len(data.encode()) <= 64, data
