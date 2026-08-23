"""/library — one-tap re-logging of the foods this person eats most.

The library fills as the user logs. This surfaces the most frequent items with
the typical portion, so a repeat meal costs a tap rather than a photo and a
vision call.
"""

from __future__ import annotations

import contextlib
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.core import tools
from umai.db.models import EntrySource, Food, FoodState, GramsSource, ResolutionMethod
from umai.db.session import session_scope
from umai.resolver.match import Resolution
from umai.telegram import keyboards
from umai.telegram.handlers.common import cb_data, cb_message, sender_id

router = Router(name="library")


# The library fills as the user logs. This surfaces the most frequent items
# for one-tap re-logging with the typical portion.


@router.message(Command("library"))
@router.message(F.text == keyboards.BTN_LIBRARY)
async def library_command(message: Message, settings: Settings, clock: Clock) -> None:
    """Show the user's most frequent foods for one-tap re-logging."""
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, sender_id(message), clock=clock)
        items = await tools.user_library(session, user.id)
    if not items:
        await message.answer(
            "Your food library is empty. Log a few meals and I'll remember the ones you eat often."
        )
        return
    await message.answer(
        "Tap to log again with your usual portion:",
        reply_markup=keyboards.library_items(items),
    )


@router.callback_query(F.data.startswith("lib:"))
async def library_quick_log(
    callback: CallbackQuery, settings: Settings, clock: Clock, models: ModelClient
) -> None:
    """One-tap log from the library. Uses the typical portion."""
    food_id_str = cb_data(callback).split(":", 1)[1]
    try:
        food_id = uuid.UUID(food_id_str)
    except ValueError:
        await callback.answer("Invalid item.", show_alert=True)
        return

    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        food = await session.get(Food, food_id)
        if food is None:
            await callback.answer("That food no longer exists.", show_alert=True)
            return

        # Get typical grams from portion priors, default to 100g
        priors = await tools.portion_priors(session, user.id)
        typical = priors.get(food.canonical_name_en, (100.0, 100.0, 100.0))[0]

        resolution = Resolution(
            food_id=food_id,
            recipe_id=None,
            display_name=food.canonical_name_en,
            method=ResolutionMethod.library,
            confidence=1.0,
        )
        meal = await tools.log_food_items(
            session,
            user.id,
            [
                tools.ItemToLog(
                    detected_name=food.canonical_name_en,
                    detected_state=FoodState(food.state.value if food.state else "unknown"),
                    grams=typical,
                    grams_source=GramsSource.user,
                    grams_confidence=1.0,
                    resolution=resolution,
                )
            ],
            occurred_at=clock.now(),
            source=EntrySource.button,
        )
    await callback.answer(f"Logged {food.canonical_name_en} ({typical:.0f}g)")
    # Update the message with the logged result
    attached = cb_message(callback)
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text(tools.format_meal(meal))
