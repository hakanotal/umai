"""/recipe — build a dish once, recognise it by name forever after.

Schema, resolver and compute all exist; this wires them to chat. Flow:
/recipe <name> → add ingredients one per message → /done → the cooked weight,
which captures evaporation → the per-100g profile.

`recipe_ingredients` stores no macros, so fixing a `foods` row retroactively
corrects every recipe built on it.
"""

from __future__ import annotations

import re
import uuid

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import tools
from umai.db.models import Food, FoodState, Recipe, RecipeIngredient
from umai.db.session import session_scope
from umai.resolver.compute import FoodLike, recipe_profile
from umai.resolver.match import Resolver
from umai.telegram.handlers.common import Awaiting
from umai.telegram.middleware import Principal

router = Router(name="recipes")

INGREDIENT_RE = re.compile(r"^(.+?)\s+(\d+(?:\.\d+)?)\s*(g|ml|gram|grams|kilogram|kg)\s*$", re.I)


@router.message(Command("recipe"))
async def recipe_command(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    """Start building a recipe. Name is required, ingredients follow."""
    text = message.text or ""
    name = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    if not name:
        await message.answer("Give the recipe a name.\nExample: /recipe my lentil soup")
        return
    await state.set_state(Awaiting.recipe_ingredients)
    await state.update_data(
        recipe_name=name,
        recipe_ingredients=[],
        recipe_cooked_grams=None,
    )
    await message.answer(
        f"Building recipe: {name}\n\n"
        "Add ingredients one at a time, like:\n"
        "  lentils 200g\n"
        "  onion 100g\n"
        "  water 500ml\n\n"
        "When done, send /done.\n"
        "To cancel, send /cancel."
    )


def parse_ingredient(text: str) -> tuple[str, float, str] | None:
    """Parse 'food_name 200g' or 'food_name 150ml' into (name, grams, state).

    Returns None if the text doesn't match the expected format.
    """
    m = INGREDIENT_RE.match(text.strip())
    if not m:
        return None
    raw_name = m.group(1).strip()
    amount = float(m.group(2))
    unit = m.group(3).lower()

    if unit in ("ml",):
        grams = amount  # density default 1.0
        state = "liquid"
    elif unit in ("kg", "kilogram"):
        grams = amount * 1000
        state = "unknown"
    else:
        grams = amount
        state = "unknown"

    return raw_name, grams, state


@router.message(Awaiting.recipe_ingredients)
async def recipe_ingredient_received(
    message: Message,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    principal: Principal,
) -> None:
    """Handle each ingredient text, or /done, or /cancel."""
    text = (message.text or "").strip()

    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("Recipe discarded.")
        return

    if text.startswith("/done"):
        data = await state.get_data()
        ingredients = data.get("recipe_ingredients", [])
        if not ingredients:
            await message.answer("Add at least one ingredient first.")
            return
        await state.update_data(recipe_cooked_grams="awaiting")
        await message.answer(
            "What does the finished dish weigh in grams?\n"
            "This captures evaporation during cooking. Send just the number, "
            "or /skip to use the raw total."
        )
        return

    data = await state.get_data()
    if data and data.get("recipe_cooked_grams") == "awaiting":
        if text.startswith("/skip"):
            cooked_grams = None
        else:
            try:
                cooked_grams = float(text.replace(",", "."))
                if cooked_grams <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Send a positive number, or /skip.")
                return
        await state.update_data(recipe_cooked_grams=cooked_grams)
        await _finish_recipe(message, state, settings, clock, principal)
        return

    parsed = parse_ingredient(text)
    if parsed is None:
        await message.answer(
            "I need a name and amount, like:\n"
            "  lentils 200g\n  onion 100g\n  water 500ml\n\n"
            "Or /done when finished."
        )
        return

    raw_name, grams, state_val = parsed
    ingredients = list(data.get("recipe_ingredients", []))
    ingredients.append({"name": raw_name, "grams": grams, "state": state_val})
    await state.update_data(recipe_ingredients=ingredients)
    n = len(ingredients)
    await message.answer(
        f"Added: {raw_name} {grams:.0f}g ({n} ingredient{'s' if n != 1 else ''})\n"
        "Next ingredient, or /done."
    )


async def _finish_recipe(
    message: Message, state: FSMContext, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Resolve all ingredients, create the Recipe row, compute per-100g."""
    data = await state.get_data()
    name = data["recipe_name"]
    raw_ingredients = data["recipe_ingredients"]
    cooked_grams = data.get("recipe_cooked_grams")

    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        resolver = Resolver(session)

        # Resolve each ingredient to a food row
        resolved: list[tuple[uuid.UUID, float]] = []
        unresolved: list[str] = []
        for ing in raw_ingredients:
            resolution = await resolver.resolve(ing["name"], FoodState(ing["state"]), user.id)
            if resolution.food_id is not None:
                resolved.append((resolution.food_id, ing["grams"]))
            else:
                unresolved.append(f"{ing['name']} {ing['grams']:.0f}g")

        if unresolved:
            await message.answer(
                "I couldn't look up these ingredients in the food table:\n"
                + "\n".join(f"  {u}" for u in unresolved)
                + "\n\nLog them first (photo or text) so the table knows them, "
                "then try again."
            )
            await state.clear()
            return

        if not resolved:
            await message.answer("No valid ingredients found.")
            await state.clear()
            return

        # Create the Recipe row

        recipe = Recipe(
            user_id=user.id,
            name=name,
            raw_input_grams=sum(g for _, g in resolved),
            cooked_output_grams=cooked_grams,
        )
        session.add(recipe)
        await session.flush()

        for food_id, grams in resolved:
            session.add(
                RecipeIngredient(
                    recipe_id=recipe.id,
                    food_id=food_id,
                    grams=grams,
                )
            )
        await session.flush()

        # Compute per-100g profile
        foods: list[tuple[FoodLike, float]] = []
        for food_id, grams in resolved:
            food = await session.get(Food, food_id)
            if food is not None:
                foods.append((food, grams))

        profile = recipe_profile(foods, cooked_output_grams=cooked_grams)
        recipe.kcal_per_100g = profile.kcal_per_100g
        recipe.protein_g_per_100g = profile.protein_g_per_100g
        recipe.carbs_g_per_100g = profile.carbs_g_per_100g
        recipe.fat_g_per_100g = profile.fat_g_per_100g

    await state.clear()
    lines = "\n".join(f"  {ing['name']} {ing['grams']:.0f}g" for ing in raw_ingredients)
    total_g = sum(ing["grams"] for ing in raw_ingredients)
    yield_note = ""
    if cooked_grams:
        yield_note = f"\nCooked: {cooked_grams:.0f}g (yield {cooked_grams / total_g:.0%})"
    await message.answer(
        f"Recipe: {name}\n\n"
        f"{lines}\n\n"
        f"Total: {total_g:.0f}g{yield_note}\n"
        f"Per 100g: {profile.kcal_per_100g:.0f} kcal, "
        f"P {profile.protein_g_per_100g:.0f}g, "
        f"C {profile.carbs_g_per_100g:.0f}g, "
        f"F {profile.fat_g_per_100g:.0f}g\n\n"
        "Now if I see this dish in a photo I'll recognise it by name."
    )
