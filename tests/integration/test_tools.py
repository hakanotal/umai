"""The logging write path and daily read path, against real Postgres.

High priority: every number the user ever sees flows through here, and the
invariants (derived macros, immutable entries, raw-versus-cooked) are the ones
the architecture depends on.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from umai.analytics import safety
from umai.clock import FakeClock
from umai.core import tools
from umai.db.models import Correction, EntryKind, Food, FoodSource, FoodState, LogEntry, UserStatus
from umai.resolver.match import Resolution, ResolutionMethod
from umai.telegram.middleware import ensure_user

pytestmark = pytest.mark.integration

CLOCK = FakeClock(dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC))


def rice_boiled() -> Food:
    return Food(
        canonical_name_en="test white rice",
        aliases=["pirinc"],
        state=FoodState.boiled,
        kcal_per_100g=130.0,
        protein_g_per_100g=2.7,
        carbs_g_per_100g=28.0,
        fat_g_per_100g=0.3,
        source=FoodSource.turkomp,
        trust_tier=1,
    )


def rice_raw() -> Food:
    return Food(
        canonical_name_en="test white rice",
        state=FoodState.raw,
        kcal_per_100g=350.0,
        protein_g_per_100g=7.5,
        carbs_g_per_100g=78.0,
        fat_g_per_100g=0.6,
        yield_factor=2.7,
        source=FoodSource.turkomp,
        trust_tier=1,
    )


def chicken_grilled() -> Food:
    return Food(
        canonical_name_en="test chicken breast",
        state=FoodState.grilled,
        kcal_per_100g=165.0,
        protein_g_per_100g=31.0,
        carbs_g_per_100g=0.0,
        fat_g_per_100g=3.6,
        source=FoodSource.usda_foundation,
        trust_tier=1,
    )


async def seed(session, *foods: Food) -> dict[str, Food]:
    out = {}
    for f in foods:
        session.add(f)
    await session.flush()
    for f in foods:
        out[f"{f.canonical_name_en}:{f.state}"] = f
    return out


def resolved(food: Food, confidence: float = 0.9) -> Resolution:
    return Resolution(
        food_id=food.id,
        recipe_id=None,
        display_name=food.canonical_name_en,
        method=ResolutionMethod.exact,
        confidence=confidence,
    )


def person(user) -> None:
    user.sex = "male"
    user.height_cm = 180.0
    user.birth_date = dt.date(1990, 5, 1)
    user.goal_rate_kg_per_week = -0.5


# --- the write path ---------------------------------------------------------


async def test_macros_come_from_the_food_row(session, user):
    foods = await seed(session, rice_boiled(), chicken_grilled())
    meal = await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="test white rice",
                detected_state=FoodState.boiled,
                grams=200,
                grams_source="vlm",
                resolution=resolved(foods["test white rice:boiled"]),
            ),
            tools.ItemToLog(
                detected_name="test chicken breast",
                detected_state=FoodState.grilled,
                grams=150,
                grams_source="vlm",
                resolution=resolved(foods["test chicken breast:grilled"]),
            ),
        ],
        occurred_at=CLOCK.now(),
        source="photo",
    )
    assert meal.total_kcal == pytest.approx(200 * 1.30 + 150 * 1.65)
    assert meal.items[0].protein_g == pytest.approx(5.4)


async def test_a_raw_row_priced_against_a_cooked_portion_uses_yield(session, user):
    """270g of boiled rice against a raw row is 100g of raw rice, or the entry
    is wrong by a factor of 2.7."""
    foods = await seed(session, rice_raw())
    meal = await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="test white rice",
                detected_state=FoodState.boiled,
                grams=270,
                grams_source="vlm",
                resolution=resolved(foods["test white rice:raw"]),
            )
        ],
        occurred_at=CLOCK.now(),
        source="photo",
    )
    assert meal.total_kcal == pytest.approx(3.5 * 350.0 / 3.5)  # 350 kcal
    assert meal.items[0].kcal == pytest.approx(350.0)


async def test_an_unmatched_item_logs_zero_macros_and_is_flagged(session, user):
    meal = await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="mystery stew",
                detected_state=FoodState.unknown,
                grams=300,
                grams_source="vlm",
                resolution=Resolution(
                    food_id=None,
                    recipe_id=None,
                    display_name="mystery stew",
                    method=ResolutionMethod.new,
                    confidence=0.0,
                ),
            )
        ],
        occurred_at=CLOCK.now(),
        source="photo",
    )
    assert meal.has_unmatched
    assert meal.total_kcal == 0.0
    text = tools.format_meal(meal)
    assert "looking it up" in text
    # The absurd total is not presented as a total. A plate whose items are all
    # unmatched showed "Total 10 kcal" in the first live session; the number is
    # arithmetically right and factually useless, so it is withheld.
    assert "Total" not in text


# --- corrections ------------------------------------------------------------


async def test_a_gram_correction_supersedes_rather_than_mutates(session, user):
    foods = await seed(session, rice_boiled())
    meal = await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="test white rice",
                detected_state=FoodState.boiled,
                grams=200,
                grams_source="vlm",
                resolution=resolved(foods["test white rice:boiled"]),
            )
        ],
        occurred_at=CLOCK.now(),
        source="photo",
    )
    from umai.db.models import FoodItem

    item_id = (
        await session.execute(select(FoodItem.id).where(FoodItem.entry_id == meal.entry.id))
    ).scalar_one()

    fixed = await tools.supersede_with_grams(session, user.id, meal.entry.id, {item_id: 300.0})

    old = await session.get(LogEntry, meal.entry.id)
    assert old.superseded_by == fixed.entry.id
    assert fixed.items[0].kcal == pytest.approx(390.0)

    # Scoped to this entry, not table-wide. A table-wide count passes only
    # against a virgin database and fails the moment the suite is pointed at a
    # dev database that has ever been used.
    corrections = (
        (await session.execute(select(Correction).where(Correction.entry_id == meal.entry.id)))
        .scalars()
        .all()
    )
    assert len(corrections) == 1
    assert corrections[0].field == "grams"


async def test_day_totals_count_only_the_newest_version(session, user):
    foods = await seed(session, rice_boiled())
    meal = await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="test white rice",
                detected_state=FoodState.boiled,
                grams=100,
                grams_source="vlm",
                resolution=resolved(foods["test white rice:boiled"]),
            )
        ],
        occurred_at=CLOCK.now(),
        source="photo",
    )
    from umai.db.models import FoodItem

    item_id = (
        await session.execute(select(FoodItem.id).where(FoodItem.entry_id == meal.entry.id))
    ).scalar_one()
    await tools.supersede_with_grams(session, user.id, meal.entry.id, {item_id: 200.0})

    totals = await tools.day_totals(session, user, CLOCK)
    assert totals.kcal == pytest.approx(260.0)  # 200g, not 100+200


# --- simple logs and the read path -------------------------------------------


async def test_water_and_weight_round_trip(session, user):
    await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.water,
        value=250,
        unit="ml",
        occurred_at=CLOCK.now(),
    )
    await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.weight,
        value=88.4,
        unit="kg",
        occurred_at=CLOCK.now(),
        source="button",
    )
    totals = await tools.day_totals(session, user, CLOCK)
    assert totals.water_ml == 250.0
    assert await tools.latest_weight(session, user.id) == pytest.approx(88.4)


async def test_an_implausible_weight_is_refused(session, user):
    with pytest.raises(ValueError, match="plausible"):
        await tools.log_simple(
            session,
            user.id,
            kind=EntryKind.weight,
            value=900,
            unit="kg",
            occurred_at=CLOCK.now(),
        )


async def test_the_static_target_respects_the_floor(session, user):
    person(user)
    decision = tools.current_target(user, 88.0, CLOCK)
    # MSJ male 88kg/180cm/36.3y = 1830, x1.4 = 2562, minus the 550 kcal
    # deficit implied by -0.5 kg/week = 2010. Computed inline rather than
    # restated, so the test and the code cannot drift apart on age.
    age = safety.age_years(dt.date(1990, 5, 1), dt.date(2026, 8, 22))
    bmr = safety.mifflin_st_jeor(sex="male", weight_kg=88.0, height_cm=180.0, age_years=age)
    expected = round(bmr * 1.4 + (-0.5 * 7700.0 / 7.0))
    assert decision.kcal_target == expected
    assert decision.protein_target_g == pytest.approx(141)


async def test_the_target_refuses_to_compute_without_person_fields(session, user):
    user.sex = None
    with pytest.raises(RuntimeError, match="person fields"):
        tools.current_target(user, 88.0, CLOCK)


async def test_ensure_user_creates_a_pending_row_and_seeds_nothing(session):
    """The replacement for `get_or_create_user`, and the point of replacing it.

    Its predecessor copied timezone, sex, height, birth date, goal rate,
    cuisines and a starting weight out of the environment onto every row it
    created, which made the second person to use the bot a clone of the first:
    their BMR, their safety floors and their local day all belonged to somebody
    else. Nothing is seeded now, and the empty fields are what the onboarding
    wizard is for.
    """
    user = await ensure_user(session, 123456)
    assert user.status == UserStatus.pending
    assert (user.tz, user.sex, user.height_cm, user.birth_date) == (None, None, None, None)
    assert list(user.cuisines) == []
    assert await tools.latest_weight(session, user.id) is None

    again = await ensure_user(session, 123456)
    assert again.id == user.id


async def test_the_bootstrap_admin_skips_the_invite_phrase(session):
    """Otherwise the first run of a fresh deployment has nobody who can admit
    anybody — including themselves."""
    user = await ensure_user(session, 999, bootstrap_admin_id=999)
    assert user.status == UserStatus.onboarding
    assert user.is_admin is True


async def test_load_user_raises_rather_than_inventing_a_person(session):
    """The middleware has always created the row before a handler runs, so an
    absent one is a broken invariant rather than a first-time user."""
    import uuid as _uuid

    with pytest.raises(RuntimeError, match="access middleware"):
        await tools.load_user(session, _uuid.uuid4())


async def test_format_day_mentions_unmatched_items(session, user):
    totals = tools.DayTotals(
        kcal=500,
        protein_g=30,
        carbs_g=40,
        fat_g=15,
        water_ml=250,
        entry_count=2,
        unmatched_items=1,
    )
    text = tools.format_day(user, totals, None)
    assert "500 kcal" in text
    assert "not yet in the food table" in text
