"""Export gives a person everything of theirs, erase leaves nothing of theirs,
and neither one touches anybody else.

The second half is what needs a real database. Erasure leans on twelve
ON DELETE CASCADE foreign keys, and two tables that do not follow — `corrections`,
which has no ON DELETE clause at all and would raise a foreign-key violation,
and `perception_runs.entry_id`, which is ON DELETE SET NULL and would leave the
raw model description of somebody's dinner behind with its owner removed. Both
are handled explicitly in `core/datarights.erase`, and this file is what stops
the next foreign key added to a per-user table from being forgotten.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select

from umai.clock import FakeClock
from umai.core import datarights, tools
from umai.db.models import (
    Correction,
    EntryKind,
    EntrySource,
    Food,
    FoodItem,
    FoodSource,
    FoodState,
    GramsSource,
    HealthMetric,
    LogEntry,
    Media,
    PerceptionRun,
    ResolutionMethod,
    User,
)
from umai.resolver.match import Resolution

CLOCK = FakeClock(dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC))


async def _populate(session, user) -> dict:
    """A user with something in every table erasure has to reach."""
    food = Food(
        id=uuid.uuid4(),
        canonical_name_en=f"rice {uuid.uuid4().hex[:8]}",
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

    meal = await tools.log_food_items(
        session,
        user.id,
        [
            tools.ItemToLog(
                detected_name="rice",
                detected_state=FoodState.boiled,
                grams=150.0,
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
        source=EntrySource.photo,
    )
    await tools.remember(session, user.id, meal)
    await tools.add_dinnerware(session, user.id, "plate", "26 cm")
    await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.weight,
        value=88.0,
        unit="kg",
        occurred_at=CLOCK.now(),
        source=EntrySource.manual,
    )
    session.add(
        HealthMetric(
            id=uuid.uuid4(),
            user_id=user.id,
            metric="steps",
            value=8000,
            unit="count",
            recorded_at=CLOCK.now(),
            source="health_auto_export",
        )
    )
    media = Media(
        id=uuid.uuid4(),
        user_id=user.id,
        entry_id=meal.entry.id,
        path=f"/tmp/{uuid.uuid4().hex}.jpg",
        sha256=uuid.uuid4().hex * 2,
    )
    session.add(media)
    await session.flush()

    # The two that do not cascade.
    item_id = (
        await session.execute(select(FoodItem.id).where(FoodItem.entry_id == meal.entry.id))
    ).scalar_one()
    session.add(
        Correction(
            id=uuid.uuid4(),
            entry_id=meal.entry.id,
            food_item_id=item_id,
            field="grams",
            old_value="150",
            new_value="200",
        )
    )
    session.add(
        PerceptionRun(
            id=uuid.uuid4(),
            media_id=media.id,
            entry_id=meal.entry.id,
            model="test-model",
            prompt_fingerprint="abc",
            raw_response={"items": [{"name": "rice"}]},
        )
    )
    await session.flush()
    return {"meal": meal, "media": media}


async def _counts(session, user_id) -> dict[str, int]:
    """Everything of one person's, by table."""

    async def n(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one())

    entry_ids = select(LogEntry.id).where(LogEntry.user_id == user_id)
    return {
        "users": await n(select(func.count(User.id)).where(User.id == user_id)),
        "entries": await n(select(func.count(LogEntry.id)).where(LogEntry.user_id == user_id)),
        "items": await n(select(func.count(FoodItem.id)).where(FoodItem.entry_id.in_(entry_ids))),
        "health": await n(
            select(func.count(HealthMetric.id)).where(HealthMetric.user_id == user_id)
        ),
        "media": await n(select(func.count(Media.id)).where(Media.user_id == user_id)),
        "corrections": await n(
            select(func.count(Correction.id)).where(Correction.entry_id.in_(entry_ids))
        ),
        "perception": await n(
            select(func.count(PerceptionRun.id)).where(PerceptionRun.entry_id.in_(entry_ids))
        ),
    }


# --- export ----------------------------------------------------------------


async def test_export_contains_the_users_own_data(session, user):
    await _populate(session, user)
    data = await datarights.export(session, user.id)

    assert data["profile"]["telegram_id"] == user.telegram_id
    assert len(data["entries"]) == 2  # the meal and the weigh-in
    assert any(e["items"] for e in data["entries"])
    assert data["health_metrics"] and data["dinnerware"] and data["photos"]


async def test_export_never_includes_another_users_data(session, user, other_user):
    await _populate(session, user)
    await _populate(session, other_user)

    mine = await datarights.export(session, user.id)
    assert {e["user_id"] for e in mine["entries"]} == {str(user.id)}
    assert {m["user_id"] for m in mine["health_metrics"]} == {str(user.id)}
    assert {m["user_id"] for m in mine["photos"]} == {str(user.id)}


async def test_the_export_withholds_the_health_token(session, user):
    """It is an access key rather than information about the person, and the
    file travels through a chat and onto a laptop."""
    user.health_token = "a-live-credential"
    await session.flush()
    data = await datarights.export(session, user.id)
    assert "health_token" not in data["profile"]


async def test_the_export_is_json_serialisable(session, user):
    """uuids, dates and Decimals all appear in these rows, and every one of
    them makes json.dumps raise."""
    import json

    await _populate(session, user)
    data = await datarights.export(session, user.id)
    assert json.loads(json.dumps(data))["profile"]["telegram_id"] == user.telegram_id


async def test_the_csv_has_a_row_per_item(session, user):
    await _populate(session, user)
    data = await datarights.export(session, user.id)
    csv = datarights.entries_csv(data)
    assert csv.splitlines()[0].startswith("occurred_at,")
    assert "rice" in csv


# --- erase -----------------------------------------------------------------


async def test_erase_removes_everything_of_theirs(session, user):
    await _populate(session, user)
    assert all(v > 0 for v in (await _counts(session, user.id)).values())

    await datarights.erase(session, user.id)

    assert (await _counts(session, user.id)) == dict.fromkeys(
        ["users", "entries", "items", "health", "media", "corrections", "perception"], 0
    )


async def test_erase_leaves_the_other_user_untouched(session, user, other_user):
    """The assertion that matters. A DELETE with a predicate one join too wide
    takes both people."""
    await _populate(session, user)
    await _populate(session, other_user)
    before = await _counts(session, other_user.id)

    await datarights.erase(session, user.id)

    assert await _counts(session, other_user.id) == before


async def test_erase_does_not_delete_the_shared_food_table(session, user):
    """`foods` belongs to the deployment, not to a person. Removing the rows
    the enrichment job wrote while researching somebody's meal would corrupt
    every other user's history, because macros are a cache recomputed from
    exactly those rows."""
    before = int((await session.execute(select(func.count(Food.id)))).scalar_one())
    await _populate(session, user)
    after_populate = int((await session.execute(select(func.count(Food.id)))).scalar_one())
    assert after_populate > before

    await datarights.erase(session, user.id)

    assert int((await session.execute(select(func.count(Food.id)))).scalar_one()) == after_populate


async def test_erasing_twice_is_not_an_error(session, user):
    """A retry after a network failure must not be a crash."""
    await _populate(session, user)
    await datarights.erase(session, user.id)
    await datarights.erase(session, user.id)
