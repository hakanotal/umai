"""User B cannot touch user A's rows.

The invariant this file guards: **every query against a per-user table carries
a `user_id` predicate.** Not "the caller checked" — the query itself, because
the ids these functions receive come out of Telegram callback data, which is
the one input in the system an attacker fully controls.

Callback data is capped at 64 bytes, so entries are addressed by the first
eight characters of their uuid. Eight hex characters is 32 bits; that is not a
secret, it is a display abbreviation, and treating it as unguessable is the
mistake this file exists to prevent.

Every test here follows the same shape: A owns something, B invokes the
operation against A's id, and the assertion is both that B is refused *and*
that A's row is untouched afterwards. The second half matters — a function that
returned None after having already written would pass the first half.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select

from umai.clock import FakeClock
from umai.core import tools
from umai.db.models import (
    EntrySource,
    Food,
    FoodItem,
    FoodSource,
    FoodState,
    GramsSource,
    LogEntry,
    ResolutionMethod,
)
from umai.resolver.match import Resolution

CLOCK = FakeClock(dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC))


async def _a_food(session) -> Food:
    """A unique row each time: `foods` is unique on (canonical_name_en, state),
    and these tests log a meal for two different users in one transaction."""
    food = Food(
        id=uuid.uuid4(),
        canonical_name_en=f"test rice {uuid.uuid4().hex[:8]}",
        state=FoodState.boiled,
        source=FoodSource.usda_foundation,
        trust_tier=1,
        kcal_per_100g=130.0,
        protein_g_per_100g=2.4,
        carbs_g_per_100g=28.0,
        fat_g_per_100g=0.3,
    )
    session.add(food)
    await session.flush()
    return food


async def _a_meal(session, user, grams: float = 150.0):
    food = await _a_food(session)
    return await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="rice",
                detected_state=FoodState.boiled,
                grams=grams,
                grams_source=GramsSource.user,
                resolution=Resolution(
                    food_id=food.id,
                    recipe_id=None,
                    display_name="rice",
                    method=ResolutionMethod.exact,
                    confidence=0.99,
                ),
            )
        ],
        occurred_at=CLOCK.now(),
        source=EntrySource.text,
    )


async def _item_id(session, entry_id) -> uuid.UUID:
    return (
        await session.execute(select(FoodItem.id).where(FoodItem.entry_id == entry_id))
    ).scalar_one()


async def _entry_snapshot(session, entry_id) -> tuple:
    """Everything a correction would change, for the "and A is untouched" half."""
    entry = await session.get(LogEntry, entry_id)
    grams = (
        await session.execute(select(func.sum(FoodItem.grams)).where(FoodItem.entry_id == entry_id))
    ).scalar_one()
    return entry.superseded_by, grams


# ---------------------------------------------------------------------------


async def test_b_cannot_supersede_as_grams_correction(session, user, other_user):
    """The one real IDOR the audit found.

    `supersede_with_grams` loaded the entry by id alone, unlike its sibling
    `hard_delete_entry` which always checked. Nothing was exploitable, because
    every caller had already scoped the id — but the safety was in the callers,
    and the next caller added would not have known to keep it.
    """
    meal = await _a_meal(session, user)
    item_id = await _item_id(session, meal.entry.id)
    before = await _entry_snapshot(session, meal.entry.id)

    result = await tools.supersede_with_grams(
        session, other_user.id, meal.entry.id, {item_id: 999.0}
    )

    assert result is None
    assert await _entry_snapshot(session, meal.entry.id) == before


async def test_b_cannot_hard_delete_as_entry(session, user, other_user):
    meal = await _a_meal(session, user)
    assert await tools.hard_delete_entry(session, other_user.id, meal.entry.id) is False
    assert await session.get(LogEntry, meal.entry.id) is not None


async def test_the_owner_can_do_both(session, user):
    """The other half of every check above: the guard refuses a stranger and
    lets the owner through. Without this the tests would pass on a function
    that refused everybody."""
    meal = await _a_meal(session, user)
    item_id = await _item_id(session, meal.entry.id)
    fixed = await tools.supersede_with_grams(session, user.id, meal.entry.id, {item_id: 200.0})
    assert fixed is not None
    assert await tools.hard_delete_entry(session, user.id, fixed.entry.id) is True


async def test_b_cannot_remove_as_dinnerware(session, user, other_user):
    await tools.add_dinnerware(session, user.id, "dinner plate", "26 cm, white")
    assert await tools.remove_dinnerware(session, other_user.id, "dinner plate") is False
    assert "dinner plate" in await tools.list_dinnerware(session, user.id)


async def test_dinnerware_lists_do_not_bleed(session, user, other_user):
    await tools.add_dinnerware(session, user.id, "dinner plate", "26 cm, white")
    await tools.add_dinnerware(session, other_user.id, "bowl", "14 cm, blue")

    assert set(await tools.list_dinnerware(session, user.id)) == {"dinner plate"}
    assert set(await tools.list_dinnerware(session, other_user.id)) == {"bowl"}


async def test_bs_entry_prefix_lookup_never_finds_as_meal(session, user, other_user):
    """The abbreviation is eight hex characters, which is a display convenience
    rather than a secret. What makes it safe is the scope on the query."""
    from umai.telegram.handlers.common import live_entry_by_prefix

    meal = await _a_meal(session, user)
    prefix = str(meal.entry.id)[:8]

    assert await live_entry_by_prefix(session, user.id, prefix) is not None
    assert await live_entry_by_prefix(session, other_user.id, prefix) is None


async def test_day_totals_count_only_the_users_own_entries(session, user, other_user):
    await _a_meal(session, user, grams=150.0)
    await _a_meal(session, other_user, grams=1500.0)

    mine = await tools.day_totals(session, user, CLOCK)
    theirs = await tools.day_totals(session, other_user, CLOCK)

    assert mine.kcal < theirs.kcal
    assert mine.entry_count == theirs.entry_count == 1


async def test_weights_do_not_bleed_between_users(session, user, other_user):
    await tools.log_weight(
        session, user, kg=88.0, occurred_at=CLOCK.now(), source=EntrySource.manual
    )
    assert await tools.latest_weight(session, user.id) == 88.0
    assert await tools.latest_weight(session, other_user.id) is None


async def test_a_library_entry_belongs_to_one_user(session, user, other_user):
    """`food_library` is what the one-tap re-log reads. Unscoped, it would
    offer you a stranger's frequent foods."""
    meal = await _a_meal(session, user)
    await tools.remember(session, user.id, meal)

    assert await tools.user_library(session, user.id, limit=5) != []
    assert await tools.user_library(session, other_user.id, limit=5) == []
