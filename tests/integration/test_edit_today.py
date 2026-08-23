"""Editing and removing today's entries, against real Postgres.

The delete path is where the schema's foreign keys all meet: media cascades
on the entry, corrections reference the entry and its items with no ON DELETE
clause, and the supersede chain points backwards at versions of the same
meal. These tests pin the behaviour the handler depends on: the row goes,
the photo archive stays, and a corrected meal does not resurrect its original
version.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from umai.clock import FakeClock
from umai.core import tools
from umai.db.models import (
    Correction,
    EntryKind,
    Food,
    FoodItem,
    FoodSource,
    FoodState,
    LogEntry,
    Media,
)
from umai.resolver.match import Resolution, ResolutionMethod

pytestmark = pytest.mark.integration

CLOCK = FakeClock(dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC))


def rice_boiled() -> Food:
    return Food(
        canonical_name_en="edit-test white rice",
        aliases=[],
        state=FoodState.boiled,
        kcal_per_100g=130.0,
        protein_g_per_100g=2.7,
        carbs_g_per_100g=28.0,
        fat_g_per_100g=0.3,
        source=FoodSource.turkomp,
        trust_tier=1,
    )


async def log_rice(session, user, grams: float = 200.0) -> tools.LoggedMeal:
    food = rice_boiled()
    session.add(food)
    await session.flush()
    return await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="edit-test white rice",
                detected_state=FoodState.boiled,
                grams=grams,
                grams_source="vlm",
                resolution=Resolution(
                    food_id=food.id,
                    recipe_id=None,
                    display_name="edit-test white rice",
                    method=ResolutionMethod.exact,
                    confidence=0.9,
                ),
            )
        ],
        occurred_at=CLOCK.now(),
        source="photo",
    )


async def _entry_with_items(session, entry_id) -> LogEntry:
    return (
        await session.execute(
            select(LogEntry).options(selectinload(LogEntry.items)).where(LogEntry.id == entry_id)
        )
    ).scalar_one()


# --- today_entries -------------------------------------------------------------


async def test_today_entries_lists_meals_and_water(session, user):
    await log_rice(session, user)
    await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.water,
        value=300.0,
        unit="ml",
        occurred_at=CLOCK.now(),
    )

    entries = await tools.today_entries(session, user, CLOCK)
    kinds = [e.kind for e in entries]
    assert EntryKind.food in kinds
    assert EntryKind.water in kinds

    water = next(e for e in entries if e.kind is EntryKind.water)
    assert water.ml == pytest.approx(300.0)
    assert "ml" in water.button_label

    meal = next(e for e in entries if e.kind is EntryKind.food)
    assert meal.kcal == pytest.approx(260.0)
    assert meal.item_count == 1
    assert "kcal" in meal.button_label
    assert len(meal.prefix) == 8


async def test_today_entries_excludes_superseded_and_other_days(session, user):
    meal = await log_rice(session, user, grams=100.0)
    items = (
        await session.execute(select(FoodItem).where(FoodItem.entry_id == meal.entry.id))
    ).scalars()
    item_id = next(items).id
    await tools.supersede_with_grams(session, meal.entry.id, {item_id: 200.0})

    # An entry from two days ago is not today's business.
    await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.water,
        value=500.0,
        unit="ml",
        occurred_at=CLOCK.now() - dt.timedelta(days=2),
    )

    entries = await tools.today_entries(session, user, CLOCK)
    assert len(entries) == 1  # the corrected meal, newest link only
    assert entries[0].kcal == pytest.approx(260.0)


# --- hard delete ---------------------------------------------------------------


async def test_hard_delete_removes_the_meal_from_totals(session, user):
    meal = await log_rice(session, user)
    assert await tools.hard_delete_entry(session, user.id, meal.entry.id) is True

    assert await session.get(LogEntry, meal.entry.id) is None
    # Scoped to this entry, not table-wide: the suite runs against a dev
    # database that has seen real use.
    n_items = (
        await session.execute(
            select(func.count()).select_from(FoodItem).where(FoodItem.entry_id == meal.entry.id)
        )
    ).scalar_one()
    assert n_items == 0
    totals = await tools.day_totals(session, user, CLOCK)
    assert totals.kcal == 0.0


async def test_hard_delete_keeps_the_photo_archive(session, user):
    meal = await log_rice(session, user)
    media = Media(
        id=uuid.uuid4(),
        user_id=user.id,
        path="/tmp/edit-test.jpg",
        sha256=uuid.uuid4().hex,
        taken_at=None,
        entry_id=meal.entry.id,
    )
    session.add(media)
    await session.flush()

    assert await tools.hard_delete_entry(session, user.id, meal.entry.id) is True

    survivor = await session.get(Media, media.id)
    assert survivor is not None  # the re-scoring archive is not collateral
    assert survivor.entry_id is None


async def test_hard_delete_clears_the_corrections_rows(session, user):
    meal = await log_rice(session, user, grams=100.0)
    items = (
        await session.execute(select(FoodItem).where(FoodItem.entry_id == meal.entry.id))
    ).scalars()
    item_id = next(items).id
    fixed = await tools.supersede_with_grams(session, meal.entry.id, {item_id: 200.0})
    # Scoped to this entry, not table-wide, same reasoning as above.
    n_corrections = (
        await session.execute(
            select(func.count()).select_from(Correction).where(Correction.entry_id == meal.entry.id)
        )
    ).scalar_one()
    assert n_corrections == 1

    assert await tools.hard_delete_entry(session, user.id, fixed.entry.id) is True

    # Both chain links gone, no resurrection of the original 100g version,
    # and the audit rows that referenced them went too.
    assert await session.get(LogEntry, meal.entry.id) is None
    assert await session.get(LogEntry, fixed.entry.id) is None
    n_corrections = (
        await session.execute(
            select(func.count()).select_from(Correction).where(Correction.entry_id == meal.entry.id)
        )
    ).scalar_one()
    assert n_corrections == 0
    totals = await tools.day_totals(session, user, CLOCK)
    assert totals.kcal == 0.0


async def test_hard_delete_is_scoped_to_the_owner(session, user):
    meal = await log_rice(session, user)
    stranger = uuid.uuid4()
    assert await tools.hard_delete_entry(session, stranger, meal.entry.id) is False
    assert await session.get(LogEntry, meal.entry.id) is not None


async def test_hard_delete_refuses_an_inner_chain_link(session, user):
    meal = await log_rice(session, user, grams=100.0)
    items = (
        await session.execute(select(FoodItem).where(FoodItem.entry_id == meal.entry.id))
    ).scalars()
    item_id = next(items).id
    fixed = await tools.supersede_with_grams(session, meal.entry.id, {item_id: 200.0})

    # The superseded original is not addressable: deleting it would corrupt
    # the live entry's chain.
    assert await tools.hard_delete_entry(session, user.id, meal.entry.id) is False
    assert await session.get(LogEntry, meal.entry.id) is not None
    assert await session.get(LogEntry, fixed.entry.id) is not None


# --- water edit ----------------------------------------------------------------


async def test_edit_water_replaces_the_amount_and_keeps_the_time(session, user):
    morning = CLOCK.now() - dt.timedelta(hours=4)
    entry = await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.water,
        value=250.0,
        unit="ml",
        occurred_at=morning,
        source="button",
    )

    new = await tools.edit_water(session, user.id, entry.id, 500.0)
    assert new is not None

    assert await session.get(LogEntry, entry.id) is None  # replaced, not mutated
    assert new.value == pytest.approx(500.0)
    assert new.occurred_at == morning  # when you drank it did not change

    totals = await tools.day_totals(session, user, CLOCK)
    assert totals.water_ml == pytest.approx(500.0)


async def test_edit_water_refuses_a_meal_entry(session, user):
    meal = await log_rice(session, user)
    assert await tools.edit_water(session, user.id, meal.entry.id, 500.0) is None
    assert await session.get(LogEntry, meal.entry.id) is not None


# --- format_entry --------------------------------------------------------------


async def test_format_entry_renders_meal_and_water(session, user):
    meal = await log_rice(session, user)
    entry = await _entry_with_items(session, meal.entry.id)
    text = tools.format_entry(user, entry)
    assert "edit-test white rice" in text
    assert "260 kcal" in text  # the same format the confirmation used

    water = await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.water,
        value=300.0,
        unit="ml",
        occurred_at=CLOCK.now(),
    )
    water_text = tools.format_entry(user, water)
    assert "300 ml" in water_text
