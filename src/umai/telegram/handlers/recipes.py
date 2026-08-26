"""/recipe — build a dish once, recognise it by name forever after.

A recipe is one `Recipe` row with N `RecipeIngredient` rows, never a row per
ingredient. The flow this module wires:

  name → list the ingredients in one message → adjust each weight → optional
  cooked weight → save. After save the dish is immutable; every correction
  lives in the adjust step, never after.

The name-first shape is why `Awaiting.recipe_name` exists: the "New Recipe"
button sets it, and this module consumes it. Before it was wired, a typed
name fell through to the text catch-all and was logged as a meal, each
detected item upserted into the library separately, and the dish never
existed as a whole — the confusion this file replaces.

The "Save as recipe" button on a logged meal (handled in `confirm.py`) feeds
the same `create_recipe` write path: it stashes the meal's resolved
`food_items` and sets `recipe_name`, so the name handler below saves a recipe
from them without re-asking for ingredients.

`recipe_ingredients` stores no macros, so fixing a `foods` row retroactively
corrects every recipe built on it. The per-100g cache on `Recipe` is derived
in `tools.create_recipe`, never typed in.

No database transaction is held across the model call that parses the
ingredient list: the user is loaded and the session closed before the
routing model splits the text, then a fresh session resolves each item.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.db.models import FoodState, Recipe
from umai.db.session import session_scope
from umai.resolver.match import Resolver
from umai.telegram import keyboards
from umai.telegram.handlers.common import Awaiting, cb_data, cb_message
from umai.telegram.middleware import Principal

router = Router(name="recipes")


# ---------------------------------------------------------------------------
# Entry points: /recipe and the New Recipe button (which sets recipe_name)
# ---------------------------------------------------------------------------


@router.message(Command("recipe"))
async def recipe_command(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    """Start building a recipe. Name may be inline or asked next."""
    text = message.text or ""
    name = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    if name:
        await state.set_state(Awaiting.recipe_ingredients)
        await state.update_data(recipe_name=name, recipe_meal_ingredients=None)
        await _ask_for_ingredients(message, name)
        return
    await state.set_state(Awaiting.recipe_name)
    await state.update_data(recipe_meal_ingredients=None)
    await message.answer("What should I call this recipe?")


@router.message(Awaiting.recipe_name, F.text)
async def recipe_name_received(
    message: Message,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    principal: Principal,
) -> None:
    """Receive the name. If a meal's ingredients are stashed (Save-as-recipe
    path), save at once; otherwise ask for the ingredient list."""
    name = (message.text or "").strip()
    if name.startswith("/cancel"):
        await state.clear()
        await message.answer("Recipe discarded.")
        return
    if not name or name.startswith("/"):
        await message.answer("Send a name for the recipe, or /cancel to discard.")
        return

    data = await state.get_data()
    meal_ingredients = data.get("recipe_meal_ingredients")
    if meal_ingredients:
        await _save_from_meal(message, state, principal, name, meal_ingredients)
        return

    await state.set_state(Awaiting.recipe_ingredients)
    await state.update_data(recipe_name=name)
    await _ask_for_ingredients(message, name)


async def _ask_for_ingredients(message: Message, name: str) -> None:
    await message.answer(
        f"Building recipe: {name}\n\n"
        "List the ingredients in one message, like:\n"
        "  yoghurt 150g, granola 50g, frozen blueberries\n\n"
        "Items without a weight default to 100g — you'll adjust them next."
    )


# ---------------------------------------------------------------------------
# The ingredient list: parse, resolve, build the adjustable draft
# ---------------------------------------------------------------------------


@router.message(Awaiting.recipe_ingredients, F.text)
async def recipe_ingredients_received(
    message: Message,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    models: ModelClient,
    principal: Principal,
) -> None:
    """Parse the whole list at once, resolve each item, show the draft.

    Typing a fresh list replaces the draft, so a restart is one message rather
    than a chain of /cancel and /recipe. The model call sits between two
    sessions: the user is loaded for the cuisine hint, the session closes,
    the routing model splits the text, then a new session resolves each item
    against the food table. No transaction is held across the call.
    """
    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Recipe discarded.")
        return

    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)

    classification = await agent.classify_text(text, user, models)
    items = classification.items
    if not items:
        await message.answer(
            "I couldn't read any ingredients. Try a list like "
            "'yoghurt 150g, granola 50g, frozen blueberries'."
        )
        return

    unresolved: list[str] = []
    draft: list[dict[str, Any]] = []
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        resolver = Resolver(session)
        for it in items:
            grams = (
                100.0 if (it["grams_estimated"] or float(it["grams"]) <= 0) else float(it["grams"])
            )
            resolution = await resolver.resolve(it["name"], FoodState(it["state"]), user.id)
            if resolution.food_id is not None:
                draft.append(
                    {
                        "food_id": str(resolution.food_id),
                        "name": it["name"],
                        "grams": grams,
                    }
                )
            else:
                unresolved.append(it["name"])

    if unresolved:
        await message.answer(
            "I couldn't find these in the food table:\n"
            + ", ".join(unresolved)
            + "\n\nLog them once as a meal first so the table knows them, "
            "then build the recipe."
        )
        return
    if not draft:
        await message.answer("No valid ingredients found. Try again.")
        return

    await state.update_data(recipe_draft=draft, recipe_cooked_grams=None)
    await message.answer(
        _draft_summary(draft, None),
        reply_markup=keyboards.recipe_draft(len(draft)),
    )


def _draft_summary(draft: list[dict[str, Any]], cooked_grams: float | None) -> str:
    lines = "\n".join(
        f"{i}. {d['name']} {float(d['grams']):.0f}g" for i, d in enumerate(draft, start=1)
    )
    total = sum(float(d["grams"]) for d in draft)
    yield_note = ""
    if cooked_grams is not None:
        yield_note = f"\nCooked: {cooked_grams:.0f}g"
    return (
        f"{lines}\n\n"
        f"Total: {total:.0f}g{yield_note}\n\n"
        "Tap ✏️ to fix a weight, ♨️ for the cooked weight, then 💾 Save recipe."
    )


# ---------------------------------------------------------------------------
# The adjust step: per-item gram fixes and the cooked-weight side-trip
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("rfix:"))
async def recipe_fix_item(callback: CallbackQuery, state: FSMContext) -> None:
    parts = cb_data(callback).split(":", 1)
    try:
        idx = int(parts[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid item.", show_alert=True)
        return
    data = await state.get_data()
    draft = data.get("recipe_draft") or []
    if not (1 <= idx <= len(draft)):
        await callback.answer("That item is gone.", show_alert=True)
        return
    await state.set_state(Awaiting.recipe_number)
    await state.update_data(mode="recipe_grams", recipe_item_index=idx)
    await callback.answer()
    attached = cb_message(callback)
    if attached is not None:
        await attached.answer(f"How many grams was item {idx}?")


@router.callback_query(F.data == "rcook:")
async def recipe_cooked_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Awaiting.recipe_number)
    await state.update_data(mode="recipe_cooked")
    await callback.answer()
    attached = cb_message(callback)
    if attached is not None:
        await attached.answer(
            "What does the finished dish weigh in grams?\n"
            "This captures evaporation. Send the number, or /skip to use the raw total."
        )


@router.message(Awaiting.recipe_number, F.text)
async def recipe_number_received(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    """One number: a gram fix for a draft item, or the cooked weight."""
    text = (message.text or "").strip()
    data = await state.get_data()
    mode = data.get("mode")

    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Recipe discarded.")
        return

    draft: list[dict[str, Any]] = data.get("recipe_draft") or []
    cooked_grams = data.get("recipe_cooked_grams")

    if mode == "recipe_cooked":
        if text.startswith("/skip"):
            cooked = None
        else:
            try:
                cooked = float(text.replace(",", "."))
                if cooked <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Send a positive number, or /skip to use the raw total.")
                return
        await state.update_data(recipe_cooked_grams=cooked)
        await state.set_state(Awaiting.recipe_ingredients)
        await message.answer(
            _draft_summary(draft, cooked),
            reply_markup=keyboards.recipe_draft(len(draft), cooked_grams=cooked),
        )
        return

    if mode == "recipe_grams":
        try:
            value = float(text.replace(",", "."))
            if value <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Just the number of grams, please.")
            return
        idx = data.get("recipe_item_index")
        if not idx or not (1 <= idx <= len(draft)):
            await state.clear()
            await message.answer("That item is gone. Start over with /recipe.")
            return
        d = draft[idx - 1]
        draft[idx - 1] = {**d, "grams": value}
        await state.update_data(recipe_draft=draft)
        await state.set_state(Awaiting.recipe_ingredients)
        await message.answer(
            _draft_summary(draft, cooked_grams),
            reply_markup=keyboards.recipe_draft(len(draft), cooked_grams=cooked_grams),
        )
        return


# ---------------------------------------------------------------------------
# Save and cancel
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "rsave:")
async def recipe_save(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    principal: Principal,
) -> None:
    """Write the recipe. After this it is immutable — no edit path exists."""
    data = await state.get_data()
    name = data.get("recipe_name")
    draft: list[dict[str, Any]] = data.get("recipe_draft") or []
    cooked_grams = data.get("recipe_cooked_grams")
    if not name or not draft:
        await callback.answer("Nothing to save. Start with /recipe.", show_alert=True)
        return
    ingredients = [(uuid.UUID(str(d["food_id"])), float(d["grams"])) for d in draft]
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        recipe = await tools.create_recipe(
            session,
            user.id,
            str(name),
            ingredients,
            cooked_output_grams=(float(cooked_grams) if cooked_grams is not None else None),
        )
    await state.clear()
    await callback.answer("Saved")
    await _send_saved_summary(callback, str(name), draft, recipe)


@router.callback_query(F.data == "rcancel:")
async def recipe_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Discarded")
    attached = cb_message(callback)
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text("Recipe discarded.")


async def _save_from_meal(
    message: Message,
    state: FSMContext,
    principal: Principal,
    name: str,
    meal_ingredients: list[dict[str, Any]],
) -> None:
    """The Save-as-recipe path: ingredients come from a logged meal, so save
    as soon as the name arrives — no adjust step, the meal's grams are final."""
    ingredients = [(uuid.UUID(str(d["food_id"])), float(d["grams"])) for d in meal_ingredients]
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        recipe = await tools.create_recipe(session, user.id, name, ingredients)
    await state.clear()
    total = sum(g for _, g in ingredients)
    await message.answer(
        f"Saved recipe: {name}\n"
        f"{len(ingredients)} ingredients, {total:.0f}g total — "
        f"{recipe.kcal_per_100g:.0f} kcal/100g.\n\n"
        "Find it in 🍽️ My Recipes, and I'll recognise it by name in photos."
    )


async def _send_saved_summary(
    callback: CallbackQuery,
    name: str,
    draft: list[dict[str, Any]],
    recipe: Recipe,
) -> None:
    total = sum(float(d["grams"]) for d in draft)
    lines = "\n".join(
        f"{i}. {d['name']} {float(d['grams']):.0f}g" for i, d in enumerate(draft, start=1)
    )
    await callback.message.answer(  # type: ignore[union-attr]
        f"Saved recipe: {name}\n\n"
        f"{lines}\n\n"
        f"Total: {total:.0f}g\n"
        f"Per 100g: {recipe.kcal_per_100g:.0f} kcal, "
        f"P {recipe.protein_g_per_100g or 0:.0f}g, "
        f"C {recipe.carbs_g_per_100g or 0:.0f}g, "
        f"F {recipe.fat_g_per_100g or 0:.0f}g\n\n"
        "Find it in 🍽️ My Recipes, and I'll recognise it by name in photos."
    )
