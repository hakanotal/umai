"""The recipe write path (`create_recipe`) and the meal-to-recipe read
(`recipe_ingredients_from_entry`), against real Postgres.

Both are the load-bearing pieces of the rebuilt feature: a recipe is one row
with N ingredients, never a row per ingredient; and a meal becomes a recipe
only when every item is a resolved food. The FSM flow that drives them is
model-gated and covered by the cassette convention; these tests pin the
invariants that hold without a model call.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from umai.clock import FakeClock
from umai.core import tools
from umai.db.models import (
    Food,
    FoodSource,
    FoodState,
    GramsSource,
    RecipeIngredient,
)
from umai.resolver.compute import recipe_profile
from umai.resolver.match import Resolution, ResolutionMethod

pytestmark = pytest.mark.integration

CLOCK = FakeClock(dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC))


def _yoghurt() -> Food:
    return Food(
        canonical_name_en="yoghurt",
        state=FoodState.unknown,
        kcal_per_100g=59.0,
        protein_g_per_100g=10.0,
        carbs_g_per_100g=3.6,
        fat_g_per_100g=0.4,
        source=FoodSource.usda_foundation,
        trust_tier=1,
    )


def _granola() -> Food:
    return Food(
        canonical_name_en="granola",
        state=FoodState.unknown,
        kcal_per_100g=471.0,
        protein_g_per_100g=10.0,
        carbs_g_per_100g=64.0,
        fat_g_per_100g=20.0,
        source=FoodSource.usda_foundation,
        trust_tier=1,
    )


async def _seed(session, *foods: Food) -> None:
    for f in foods:
        session.add(f)
    await session.flush()


def _resolved(food: Food) -> Resolution:
    return Resolution(
        food_id=food.id,
        recipe_id=None,
        display_name=food.canonical_name_en,
        method=ResolutionMethod.exact,
        confidence=1.0,
    )


async def _log_meal(
    session,
    user_id,
    items: list[tools.ItemToLog],
) -> tools.LoggedMeal:
    return await tools.log_food_items(
        session,
        user_id,
        items,
        occurred_at=CLOCK.now(),
        source="text",
    )


# --- create_recipe: one row, N ingredients, derived per-100g ---------------


async def test_create_recipe_writes_one_recipe_with_ingredients(session, user):
    """The dish exists as a whole: one Recipe, N RecipeIngredient rows, never a
    row per ingredient (the confusion this replaces)."""
    yog, gran = _yoghurt(), _granola()
    await _seed(session, yog, gran)

    recipe = await tools.create_recipe(
        session,
        user.id,
        "morning yogurt",
        [(yog.id, 150.0), (gran.id, 50.0)],
    )

    assert recipe.name == "morning yogurt"
    assert recipe.user_id == user.id
    assert recipe.raw_input_grams == 200.0
    # Per-100g cache is derived, matches the pure-code computation exactly.
    expected = recipe_profile([(yog, 150.0), (gran, 50.0)])
    assert recipe.kcal_per_100g == pytest.approx(expected.kcal_per_100g)
    assert recipe.protein_g_per_100g == pytest.approx(expected.protein_g_per_100g)

    ingredients = (
        (
            await session.execute(
                select(RecipeIngredient).where(RecipeIngredient.recipe_id == recipe.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(ingredients) == 2
    by_food = {ing.food_id: ing.grams for ing in ingredients}
    assert by_food == {yog.id: 150.0, gran.id: 50.0}


async def test_create_recipe_cooked_weight_captures_evaporation(session, user):
    """Cooked output below the raw total raises per-100g: 1400g in, 1100g out."""
    rice = Food(
        canonical_name_en="rice raw",
        state=FoodState.raw,
        kcal_per_100g=350.0,
        protein_g_per_100g=7.0,
        carbs_g_per_100g=78.0,
        fat_g_per_100g=0.6,
        yield_factor=2.7,
        source=FoodSource.turkomp,
        trust_tier=1,
    )
    await _seed(session, rice)

    recipe = await tools.create_recipe(
        session,
        user.id,
        "soup",
        [(rice.id, 1400.0)],
        cooked_output_grams=1100.0,
    )
    # 1400g of raw rice at 350 kcal/100g = 4900 kcal total, over 1100g cooked
    # = ~445 kcal/100g. Without the cooked weight it would be 350.
    assert recipe.kcal_per_100g > 350.0
    assert recipe.cooked_output_grams == 1100.0


async def test_create_recipe_appears_in_user_recipes_for_owner_only(session, user, other_user):
    """The write is scoped to the caller: a stranger's list never sees it."""
    yog = _yoghurt()
    await _seed(session, yog)
    await tools.create_recipe(session, user.id, "mine", [(yog.id, 100.0)])

    mine = await tools.user_recipes(session, user.id, limit=5)
    theirs = await tools.user_recipes(session, other_user.id, limit=5)
    assert len(mine) == 1
    assert mine[0].name == "mine"
    assert theirs == []


async def test_create_recipe_is_logged_as_a_whole_via_recipe_id(session, user):
    """Logging the saved recipe produces one FoodItem with recipe_id set and
    food_id null — the dish, not its parts."""
    yog, gran = _yoghurt(), _granola()
    await _seed(session, yog, gran)
    recipe = await tools.create_recipe(
        session, user.id, "morning yogurt", [(yog.id, 150.0), (gran.id, 50.0)]
    )

    meal = await _log_meal(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="morning yogurt",
                detected_state=FoodState("unknown"),
                grams=200.0,
                grams_source=GramsSource.user,
                grams_confidence=1.0,
                resolution=Resolution(
                    food_id=None,
                    recipe_id=recipe.id,
                    display_name="morning yogurt",
                    method=ResolutionMethod.recipe,
                    confidence=1.0,
                ),
            )
        ],
    )
    # The whole recipe prices at its per-100g cache: 200g of the dish.
    assert meal.total_kcal > 0
    assert meal.items[0].from_recipe


# --- recipe_ingredients_from_entry: the Save-as-recipe gate ----------------


async def test_recipe_ingredients_from_entry_returns_resolved_pairs(session, user):
    """A meal of resolved foods yields the (food_id, grams) pairs ready to
    become a recipe — the Save-as-recipe path's read."""
    yog, gran = _yoghurt(), _granola()
    await _seed(session, yog, gran)
    meal = await _log_meal(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="yoghurt",
                detected_state=FoodState("unknown"),
                grams=150.0,
                grams_source=GramsSource.user,
                resolution=_resolved(yog),
            ),
            tools.ItemToLog(
                detected_name="granola",
                detected_state=FoodState("unknown"),
                grams=50.0,
                grams_source=GramsSource.user,
                resolution=_resolved(gran),
            ),
        ],
    )
    pairs = await tools.recipe_ingredients_from_entry(session, user.id, meal.entry.id)
    assert pairs == [(yog.id, 150.0), (gran.id, 50.0)]


async def test_recipe_ingredients_from_entry_refuses_unresolved_item(session, user):
    """A meal with an item the resolver missed cannot become a recipe:
    `recipe_ingredients.food_id` is not null, and inventing nutrition is a
    tier-4 row that arrives only with the enrichment path."""
    yog = _yoghurt()
    await _seed(session, yog)
    meal = await _log_meal(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="yoghurt",
                detected_state=FoodState("unknown"),
                grams=150.0,
                grams_source=GramsSource.user,
                resolution=_resolved(yog),
            ),
            tools.ItemToLog(
                detected_name="mystery food",
                detected_state=FoodState("unknown"),
                grams=50.0,
                grams_source=GramsSource.user,
                resolution=None,
            ),
        ],
    )
    assert await tools.recipe_ingredients_from_entry(session, user.id, meal.entry.id) is None


async def test_recipe_ingredients_from_entry_refuses_recipe_sourced_meal(session, user):
    """A meal already logged from a recipe would make a recipe of a recipe,
    pricing against itself. Refused before anything is written."""
    yog = _yoghurt()
    await _seed(session, yog)
    recipe = await tools.create_recipe(session, user.id, "first", [(yog.id, 100.0)])
    meal = await _log_meal(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="first",
                detected_state=FoodState("unknown"),
                grams=200.0,
                grams_source=GramsSource.user,
                resolution=Resolution(
                    food_id=None,
                    recipe_id=recipe.id,
                    display_name="first",
                    method=ResolutionMethod.recipe,
                    confidence=1.0,
                ),
            )
        ],
    )
    assert await tools.recipe_ingredients_from_entry(session, user.id, meal.entry.id) is None


async def test_recipe_ingredients_from_entry_scoped_to_owner(session, user, other_user):
    """The entry lookup carries the user_id predicate: a stranger's prefix
    resolves to nothing, the same way every per-user query does."""
    yog = _yoghurt()
    await _seed(session, yog)
    meal = await _log_meal(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="yoghurt",
                detected_state=FoodState("unknown"),
                grams=150.0,
                grams_source=GramsSource.user,
                resolution=_resolved(yog),
            )
        ],
    )
    assert await tools.recipe_ingredients_from_entry(session, other_user.id, meal.entry.id) is None
