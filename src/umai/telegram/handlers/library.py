"""/library — one-tap re-logging of the recipes this person has saved.

Only saved recipes appear. The library of foods-logged-often — which surfaced
each logged ingredient as its own row and made a named dish invisible — was
the confusion this replaces. The `food_library` table still fills as the user
logs (it feeds the perception prompt's portion priors), it just no longer has
a display surface here.
"""

from __future__ import annotations

import contextlib
import uuid

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import tools
from umai.db.models import EntrySource, FoodState, GramsSource, Recipe, ResolutionMethod
from umai.db.session import session_scope
from umai.resolver.match import Resolution
from umai.telegram import keyboards
from umai.telegram.handlers.common import cb_data, cb_message
from umai.telegram.middleware import Principal

router = Router(name="library")


@router.message(Command("library"))
@router.message(F.text == keyboards.BTN_LIBRARY)
async def library_command(
    message: Message, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Show saved recipes for one-tap re-logging."""
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        recipes = await tools.user_recipes(session, user.id, limit=8)
    if not recipes:
        await message.answer(
            "You haven't saved any recipes yet. Create one with /recipe, "
            "or tap 💾 Save as recipe under a meal you've just logged."
        )
        return
    await message.answer(
        "Tap a recipe to log it again:",
        reply_markup=keyboards.library_items(recipes),
    )


@router.callback_query(F.data.startswith("rec:"))
async def recipe_quick_log(
    callback: CallbackQuery,
    clock: Clock,
    principal: Principal,
) -> None:
    """One-tap log of a saved recipe. Uses the most-recent portion.

    Recipes are per-user, so the `user_id` predicate is on the lookup itself,
    not just the caller: a crafted `rec:` callback with a stranger's recipe id
    matches no row and is refused before anything is written.
    """
    parts = cb_data(callback).split(":", 2)
    try:
        recipe_id = uuid.UUID(parts[1])
        grams = float(parts[2]) if len(parts) > 2 else 100.0
    except (ValueError, IndexError):
        await callback.answer("Invalid item.", show_alert=True)
        return

    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        recipe = (
            await session.execute(
                select(Recipe).where(Recipe.id == recipe_id, Recipe.user_id == user.id)
            )
        ).scalar_one_or_none()
        if recipe is None:
            await callback.answer("That recipe no longer exists.", show_alert=True)
            return

        resolution = Resolution(
            food_id=None,
            recipe_id=recipe_id,
            display_name=recipe.name,
            method=ResolutionMethod.recipe,
            confidence=1.0,
        )
        meal = await tools.log_food_items(
            session,
            user.id,
            [
                tools.ItemToLog(
                    detected_name=recipe.name,
                    detected_state=FoodState("unknown"),
                    grams=grams,
                    grams_source=GramsSource.user,
                    grams_confidence=1.0,
                    resolution=resolution,
                )
            ],
            occurred_at=clock.now(),
            source=EntrySource.button,
        )
    await callback.answer(f"Logged {recipe.name} ({grams:.0f}g)")
    attached = cb_message(callback)
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text(tools.format_meal(meal))
