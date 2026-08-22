"""Inline keyboards. The one-tap paths that decide whether logging is pleasant."""

from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from umai.core.cuisines import CUISINES


def quick_actions() -> InlineKeyboardMarkup:
    """The persistent one-tap row: water, weigh-in, today."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💧 250ml", callback_data="water:250"),
                InlineKeyboardButton(text="⚖️ weigh", callback_data="weigh"),
                InlineKeyboardButton(text="📊 today", callback_data="today"),
            ]
        ]
    )


def meal_actions(entry_id: str, n_items: int) -> InlineKeyboardMarkup:
    """Per-item gram correction under a logged meal.

    One button per item, so the only correction the plan says is usually
    needed ("that was more like 200g") is two taps: the item, then the number.
    """
    row = [
        InlineKeyboardButton(text=f"✏️ {i}", callback_data=f"fix:{entry_id[:8]}:{i}")
        for i in range(1, min(n_items, 8) + 1)
    ]
    rows = [row[i : i + 4] for i in range(0, len(row), 4)]
    rows.append([InlineKeyboardButton(text="✅ ok", callback_data=f"ok:{entry_id[:8]}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cuisines(selected: Iterable[str]) -> InlineKeyboardMarkup:
    """The cuisine picker: every option, ticked ones first in the label.

    A toggle grid rather than a typed list. What the user eats is perception
    context — it reaches the vision model as a hint — so it has to be trivially
    editable, and a free-text field would collect typos that quietly degrade
    every photo.
    """
    chosen = set(selected)
    buttons = [
        InlineKeyboardButton(
            text=("✅ " if slug in chosen else "") + label,
            callback_data=f"cuisine:{slug}",
        )
        for slug, (label, _) in CUISINES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="done", callback_data="cuisine_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
