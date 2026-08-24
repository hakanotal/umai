"""Keyboards. The one-tap paths that decide whether logging is pleasant.

Telegram gives a bot no colours and no fonts. The visual identity it *can*
have is a small emoji vocabulary used the same way in every message, and a
clear split between the two keyboard kinds:

  * The reply keyboard (main_menu) is the persistent surface: always visible,
    one tap sends its text. It carries the repetitive actions, water and
    weigh-ins and today's summary, so they never need typing.
  * Inline keyboards are contextual: attached to the message they act on,
    they vanish with it. Meal confirmations, the cuisine picker, the
    edit/remove flow.

The emoji constants exist so the vocabulary is defined once. A 💧 in one
message and a 🥤 in another is the bot equivalent of two colour themes.

Callback data is capped at 64 bytes by Telegram, which is why entries are
addressed by the first 8 characters of their uuid.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from umai.core.cuisines import CUISINES
from umai.core.tools import LibraryItem, TodayEntry

WATER = "💧"
SCALE = "⚖️"
CHART = "📊"
CALENDAR = "📅"
PENCIL = "✏️"
BASKET = "🗑"
CHECK = "✅"
HOURGLASS = "⏳"
BOOK = "📚"
GEAR = "⚙️"

WATER_250 = f"{WATER} 250 ml"
BTN_TODAY = f"{CALENDAR} Today"
BTN_WEEK = f"{CHART} Week"
BTN_EDIT = f"{PENCIL} Edit"
BTN_WEIGH = f"{SCALE} Weigh in"
BTN_LIBRARY = "🍽️ My Recipes"
BTN_CONFIGURE = f"{GEAR} Configure"

ENTRY_PREFIX_LEN = 8


def main_menu() -> ReplyKeyboardMarkup:
    """The persistent menu. Sent on /start and /help, and it stays.

    Row 1: data inputs (log water, weigh in, re-log from library).
    Row 2: viewing (today's totals, edit entries, week summary).
    Row 3: configure (opens an inline keyboard with recipe/dinnerware/cuisines).
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=WATER_250),
                KeyboardButton(text=BTN_LIBRARY),
                KeyboardButton(text=BTN_WEIGH),
            ],
            [
                KeyboardButton(text=BTN_EDIT),
                KeyboardButton(text=BTN_TODAY),
                KeyboardButton(text=BTN_WEEK),
            ],
            [KeyboardButton(text=BTN_CONFIGURE)],
        ],
        input_field_placeholder="…or just type what you ate",
        resize_keyboard=True,
    )


def configure_menu() -> InlineKeyboardMarkup:
    """The inline keyboard shown when the Configure button is tapped."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 New Recipe", callback_data="cfg:recipe"),
                InlineKeyboardButton(text="🥣 Dinnerware", callback_data="cfg:dinnerware"),
            ],
            [
                InlineKeyboardButton(text="🌍 Cuisines", callback_data="cfg:cuisines"),
                InlineKeyboardButton(text="📱 Health sync", callback_data="cfg:token"),
            ],
            [
                InlineKeyboardButton(text="💧 Water target", callback_data="cfg:water"),
            ],
        ]
    )


def meal_actions(entry_id: str, n_items: int) -> InlineKeyboardMarkup:
    """Per-item gram correction under a logged meal.

    One button per item, so the only correction the plan says is usually
    needed ("that was more like 200g") is two taps: the item, then the number.
    """
    row = [
        InlineKeyboardButton(
            text=f"{PENCIL} {i}",
            callback_data=f"fix:{entry_id[:ENTRY_PREFIX_LEN]}:{i}",
        )
        for i in range(1, min(n_items, 8) + 1)
    ]
    rows = [row[i : i + 4] for i in range(0, len(row), 4)]
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{CHECK} Looks right",
                callback_data=f"ok:{entry_id[:ENTRY_PREFIX_LEN]}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_list(entries: Sequence[TodayEntry]) -> InlineKeyboardMarkup:
    """Today's entries as buttons, oldest first so the list reads like the day.

    Meals and water both appear: water is logged more often than anything
    else and is exactly the thing a mis-tap needs to undo.
    """
    rows = [
        [InlineKeyboardButton(text=e.button_label, callback_data=f"edit:{e.prefix}")]
        for e in entries
    ]
    rows.append([InlineKeyboardButton(text="Close", callback_data="editclose")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def summary_actions() -> InlineKeyboardMarkup:
    """The one button a summary needs: straight into today's edit list."""
    button = InlineKeyboardButton(text=f"{PENCIL} Edit today", callback_data="editlist")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def edit_meal(entry_prefix: str, n_items: int) -> InlineKeyboardMarkup:
    """The meal's own edit view: fix grams per item, or remove the whole meal."""
    row = [
        InlineKeyboardButton(text=f"{PENCIL} {i}", callback_data=f"fix:{entry_prefix}:{i}")
        for i in range(1, min(n_items, 8) + 1)
    ]
    rows = [row[i : i + 4] for i in range(0, len(row), 4)]
    rows.append(
        [
            InlineKeyboardButton(text=f"{BASKET} Remove meal", callback_data=f"del:{entry_prefix}"),
            InlineKeyboardButton(text="↩ Back", callback_data="editlist"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_water(entry_prefix: str) -> InlineKeyboardMarkup:
    """Water: change the amount, or remove it."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{PENCIL} Change amount", callback_data=f"waterfix:{entry_prefix}"
                ),
                InlineKeyboardButton(text=f"{BASKET} Remove", callback_data=f"del:{entry_prefix}"),
            ],
            [InlineKeyboardButton(text="↩ Back", callback_data="editlist")],
        ]
    )


def delete_confirm(entry_prefix: str) -> InlineKeyboardMarkup:
    """Removal is one tap away from done, so the confirm is explicit."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{CHECK} Yes, remove", callback_data=f"delyes:{entry_prefix}"
                ),
                InlineKeyboardButton(text="Cancel", callback_data=f"edit:{entry_prefix}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------
#
# The wizard's keyboards carry an `ob:` prefix so its callbacks cannot collide
# with a feature router's: the two live behind opposite access filters and a
# shared prefix would be a bug that only appears mid-wizard.


def onboarding_sex() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Male", callback_data="ob:sex:male"),
                InlineKeyboardButton(text="Female", callback_data="ob:sex:female"),
            ]
        ]
    )


def onboarding_goal(choices: Sequence[tuple[str, float, str]]) -> InlineKeyboardMarkup:
    """One button per offered rate, one per row.

    Buttons rather than a typed number, deliberately. People type "1.5", the
    safety rails cap it at 1% of body weight a week, and the target they are
    then given is not the one they asked for — with no moment at which anybody
    said so. Offering only rates that survive the rails makes the limit visible
    before it applies instead of silent afterwards.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"ob:goal:{rate}")]
            for label, rate, _kind in choices
        ]
    )


def onboarding_timezones(zones: Sequence[str]) -> InlineKeyboardMarkup:
    """Candidate zones, when a city resolves to more than one."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=zone, callback_data=f"ob:tz:{zone}")] for zone in zones
        ]
    )


def onboarding_confirm_tz(zone: str) -> InlineKeyboardMarkup:
    """Yes/no on the local time echoed back.

    The one extra tap in the whole wizard, and it earns its place: a wrong
    timezone is invisible until a day lands on the wrong date weeks later, and
    every total the bot will ever show is computed from it. Checking a clock is
    the only way a person can tell at the moment they answer.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{CHECK} Yes", callback_data=f"ob:tzok:{zone}"),
                InlineKeyboardButton(text="No", callback_data="ob:tzno"),
            ]
        ]
    )


def onboarding_cuisines(selected: Iterable[str]) -> InlineKeyboardMarkup:
    """The cuisine grid again, with the wizard's own callback prefix.

    Same layout, same labels; only the prefix differs, because the feature
    router that owns `cuisine:` sits behind the active-user filter and cannot
    be reached by someone still in the wizard.
    """
    return _cuisine_grid(selected, prefix="ob:cuisine", done="ob:cuisines_done")


def cuisines(selected: Iterable[str]) -> InlineKeyboardMarkup:
    """The cuisine picker: every option, ticked ones first in the label.

    A toggle grid rather than a typed list. What the user eats is perception
    context, it reaches the vision model as a hint, so it has to be trivially
    editable, and a free-text field would collect typos that quietly degrade
    every photo.
    """
    return _cuisine_grid(selected, prefix="cuisine", done="cuisine_done")


def _cuisine_grid(selected: Iterable[str], *, prefix: str, done: str) -> InlineKeyboardMarkup:
    """The grid both cuisine pickers draw. One layout, two callback prefixes."""
    chosen = set(selected)
    buttons = [
        InlineKeyboardButton(
            text=(f"{CHECK} " if slug in chosen else "") + label,
            callback_data=f"{prefix}:{slug}",
        )
        for slug, (label, _) in CUISINES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="Done", callback_data=done)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dinnerware_list(items: dict[str, str]) -> InlineKeyboardMarkup:
    """Each dinnerware item as a remove button. Tap to delete."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{BASKET} {name}: {desc}",
                callback_data=f"dw:{name}",
            )
        ]
        for name, desc in items.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def library_items(items: list[LibraryItem]) -> InlineKeyboardMarkup:
    """One-tap re-log for frequent foods. Shows name and typical grams."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{item.name} ({item.typical_grams:.0f}g)",
                callback_data=f"lib:{item.food_id}:{item.typical_grams:.0f}",
            )
        ]
        for item in items
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
