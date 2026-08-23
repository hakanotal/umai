"""The enrichment job against a real database.

Covers the two halves the unit tests cannot: which gaps the job picks up, and
what backfilling one actually does to the rows that were waiting on it. No
model is called — `research` is the only part that talks to a provider, and it
is exercised through its validator in tests/unit/test_enrichment.py.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from umai.clock import FakeClock
from umai.core import enrichment, tools
from umai.db.models import (
    EnrichmentAttempt,
    Food,
    FoodItem,
    FoodSource,
    FoodState,
    LogEntry,
    ResolutionMethod,
)
from umai.resolver.match import Resolution

CLOCK = FakeClock(dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC))


def unmatched(name: str, grams: float, state: FoodState = FoodState.baked) -> tools.ItemToLog:
    return tools.ItemToLog(
        detected_name=name,
        detected_state=state,
        grams=grams,
        grams_source="vlm",
        resolution=Resolution(
            food_id=None,
            recipe_id=None,
            display_name=name,
            method=ResolutionMethod.new,
            confidence=0.0,
        ),
    )


async def log(session, user, *items: tools.ItemToLog):
    return await tools.log_food_items(
        session, user.id, list(items), occurred_at=CLOCK.now(), source="photo"
    )


async def test_unmatched_items_surface_as_gaps_most_eaten_first(session, user):
    await log(session, user, unmatched("lahmacun", 220), unmatched("cacik", 150))
    await log(session, user, unmatched("lahmacun", 200))
    await session.flush()

    gaps = await enrichment.pending_gaps(session, limit=10)
    names = [g.detected_name for g in gaps]

    assert "lahmacun" in names and "cacik" in names
    # Frequency is the whole scheduling policy: what you eat weekly earns the
    # expensive model's attention before a one-off side dish.
    assert names.index("lahmacun") < names.index("cacik")
    assert next(g for g in gaps if g.detected_name == "lahmacun").occurrences == 2


async def test_a_matched_item_is_not_a_gap(session, user):
    food = Food(
        canonical_name_en="test bulgur pilav",
        aliases=[],
        state=FoodState.boiled,
        kcal_per_100g=150.0,
        protein_g_per_100g=4.0,
        carbs_g_per_100g=30.0,
        fat_g_per_100g=1.0,
        source=FoodSource.usda_foundation,
        trust_tier=1,
    )
    session.add(food)
    await session.flush()

    await log(
        session,
        user,
        tools.ItemToLog(
            detected_name="test bulgur pilav",
            detected_state=FoodState.boiled,
            grams=200,
            grams_source="vlm",
            resolution=Resolution(
                food_id=food.id,
                recipe_id=None,
                display_name="test bulgur pilav",
                method=ResolutionMethod.exact,
                confidence=1.0,
            ),
        ),
    )
    await session.flush()

    gaps = await enrichment.pending_gaps(session, limit=50)
    assert "test bulgur pilav" not in [g.detected_name for g in gaps]


async def test_a_superseded_entry_stops_being_a_gap(session, user):
    """A correction that removed an item must not leave the job researching it
    forever: the gap query follows the supersede chain like every other read."""
    meal = await log(session, user, unmatched("test phantom dish", 120))
    await session.flush()
    item_id = (
        await session.execute(select(FoodItem.id).where(FoodItem.entry_id == meal.entry.id))
    ).scalar_one()

    await tools.supersede_with_grams(session, meal.entry.id, {item_id: 0.0})
    await session.flush()

    gaps = await enrichment.pending_gaps(session, limit=50)
    assert "test phantom dish" not in [g.detected_name for g in gaps]


async def test_backfill_prices_every_waiting_item_and_leaves_the_entry_alone(session, user):
    meal_one = await log(session, user, unmatched("test lahmacun", 220))
    meal_two = await log(session, user, unmatched("test lahmacun", 110))
    await session.flush()

    candidate = enrichment.validate(
        {
            "is_food": True,
            "canonical_name_en": "test lahmacun",
            "aliases": ["turkish pizza"],
            "state": "baked",
            "kcal_per_100g": 250.0,
            "protein_g_per_100g": 11.0,
            "carbs_g_per_100g": 32.0,
            "fat_g_per_100g": 8.0,
            "confidence": 0.8,
            "basis": "dough, minced lamb, onion",
        },
        expected_state=FoodState.baked,
    )
    food = await enrichment.upsert_food(session, candidate)
    await session.flush()

    # Tier 4 and only tier 4, however confident the model sounded.
    assert food.trust_tier == 4
    assert food.source is FoodSource.model
    assert food.is_provisional
    assert food.verified_at is None

    gap = enrichment.Gap("test lahmacun", FoodState.baked, 2)
    n = await enrichment.backfill(session, gap, food)
    await session.flush()

    assert n == 2
    items = (
        (
            await session.execute(
                select(FoodItem)
                .join(LogEntry, FoodItem.entry_id == LogEntry.id)
                .where(LogEntry.id.in_([meal_one.entry.id, meal_two.entry.id]))
                .order_by(FoodItem.grams.desc())
            )
        )
        .scalars()
        .all()
    )
    assert [i.kcal for i in items] == [pytest.approx(550.0), pytest.approx(275.0)]
    assert all(i.food_id == food.id for i in items)
    # Grams are untouched: the backfill prices what was logged, it does not
    # relitigate how much of it there was.
    assert [i.grams for i in items] == [220.0, 110.0]
    # And no entry was superseded — macros are a cache, entries are immutable.
    entries = (
        (
            await session.execute(
                select(LogEntry).where(LogEntry.id.in_([meal_one.entry.id, meal_two.entry.id]))
            )
        )
        .scalars()
        .all()
    )
    assert all(e.superseded_by is None for e in entries)


async def test_the_day_total_stops_being_a_floor_once_backfilled(session, user):
    """The end-to-end point of the whole module."""
    await log(session, user, unmatched("test menemen", 300))
    await session.flush()

    before = await tools.day_totals(session, user, CLOCK, CLOCK.now().date())
    assert before.kcal == 0.0
    assert before.unmatched_items == 1

    candidate = enrichment.validate(
        {
            "is_food": True,
            "canonical_name_en": "test menemen",
            "aliases": [],
            "state": "fried",
            "kcal_per_100g": 120.0,
            "protein_g_per_100g": 6.0,
            "carbs_g_per_100g": 5.0,
            "fat_g_per_100g": 8.0,
            "confidence": 0.7,
            "basis": "eggs, tomato, pepper, oil",
        },
        expected_state=FoodState.baked,
    )
    food = await enrichment.upsert_food(session, candidate)
    await enrichment.backfill(session, enrichment.Gap("test menemen", FoodState.baked, 1), food)
    await session.flush()

    after = await tools.day_totals(session, user, CLOCK, CLOCK.now().date())
    assert after.kcal == pytest.approx(360.0)
    assert after.unmatched_items == 0


async def test_an_exhausted_name_is_not_researched_again(session, user):
    """Each attempt costs a call to the most expensive model configured, so a
    dish that has failed the validator three times is left alone."""
    await log(session, user, unmatched("test unidentifiable smear", 90))
    session.add(
        EnrichmentAttempt(
            detected_name="test unidentifiable smear",
            state=FoodState.baked,
            attempts=enrichment.MAX_ATTEMPTS,
            last_error="Atwater check failed",
        )
    )
    await session.flush()

    gaps = await enrichment.pending_gaps(session, limit=50)
    assert "test unidentifiable smear" not in [g.detected_name for g in gaps]


async def test_upsert_never_overwrites_a_better_row(session, user):
    """A tier-1 lab row must survive a tier-4 guess arriving later under the
    same name."""
    session.add(
        Food(
            canonical_name_en="test overlap dish",
            aliases=[],
            state=FoodState.baked,
            kcal_per_100g=100.0,
            protein_g_per_100g=5.0,
            carbs_g_per_100g=15.0,
            fat_g_per_100g=2.0,
            source=FoodSource.usda_foundation,
            trust_tier=1,
        )
    )
    await session.flush()

    candidate = enrichment.validate(
        {
            "is_food": True,
            "canonical_name_en": "test overlap dish",
            "aliases": [],
            "state": "baked",
            "kcal_per_100g": 400.0,
            "protein_g_per_100g": 10.0,
            "carbs_g_per_100g": 40.0,
            "fat_g_per_100g": 20.0,
            "confidence": 0.9,
            "basis": "a guess",
        },
        expected_state=FoodState.baked,
    )
    food = await enrichment.upsert_food(session, candidate)

    assert food.trust_tier == 1
    assert food.kcal_per_100g == 100.0
