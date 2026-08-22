"""Tool implementations available to the agent and to the resolver.

None of them are the language model doing sums.

This module owns the logging write path and the daily read path. Invariants it
exists to honour:

  * Macros are derived, never authored. Every macro column written here comes
    out of resolver.compute against a foods row; there is no code path that
    types a calorie number in.
  * Entries are immutable. A gram correction creates a new entry that
    supersedes the original, and a Correction row records what changed, so
    explain-why and later re-analysis stay possible.
  * The raw-versus-cooked decision is made here, once, where both the detected
    state and the row state are known. Getting it wrong silently is a threefold
    error on staples, which is why it is not left to the caller.

Phase 1 scope, deliberately: targets are the static Mifflin-St Jeor estimate
(plan section 7) and summary replies are formatted in code rather than narrated
by the coach model.

An item the resolver cannot match is still logged with zero macros — nothing
here ever types a calorie number in — but that is now a *temporary* state
rather than a permanent one. `core/enrichment.py` researches the gap in the
background, writes a tier-4 row, and backfills the waiting items. Until it
does, the reply says the total is incomplete rather than presenting a number
that is arithmetically correct and factually absurd: the first live session
reported a plate of lahmacun as "Total 10 kcal" because the onion garnish was
the only thing the table could match.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from umai.analytics import safety
from umai.clock import Clock, day_bounds, today
from umai.config.settings import Settings
from umai.db.models import (
    Correction,
    EntryKind,
    EntrySource,
    Food,
    FoodItem,
    FoodState,
    GramsSource,
    LogEntry,
    Media,
    ResolutionMethod,
    User,
)
from umai.resolver import compute as compute_mod
from umai.resolver.compute import RecipeProfile
from umai.resolver.match import Resolution

# ---------------------------------------------------------------------------
# The shared write path for every food log, photo or text
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ItemToLog:
    """One perceived or typed item, ready to be written."""

    detected_name: str
    detected_state: FoodState
    grams: float
    grams_source: GramsSource
    grams_confidence: float | None = None
    resolution: Resolution | None = None


@dataclass(slots=True)
class LoggedItem:
    name: str
    grams: float
    kcal: float
    protein_g: float
    unmatched: bool
    from_recipe: bool = False


@dataclass(slots=True)
class LoggedMeal:
    entry: LogEntry
    items: list[LoggedItem] = field(default_factory=list)

    @property
    def total_kcal(self) -> float:
        return sum(i.kcal for i in self.items)

    @property
    def has_unmatched(self) -> bool:
        return any(i.unmatched for i in self.items)


async def log_food_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    items: Sequence[ItemToLog],
    *,
    occurred_at: dt.datetime,
    source: EntrySource,
    note: str | None = None,
    raw_input_ref: str | None = None,
    entry_id_to_supersede: uuid.UUID | None = None,
) -> LoggedMeal:
    """Create one log entry with its food items, macros computed in code.

    The only write path for a food log. Photo, text and correction all land
    here so the derived-macro and immutability invariants have one place to
    live.
    """
    entry = LogEntry(
        user_id=user_id,
        occurred_at=occurred_at,
        kind=EntryKind.food,
        source=source,
        note=note,
        raw_input_ref=raw_input_ref,
    )
    session.add(entry)
    await session.flush()

    logged: list[LoggedItem] = []

    for position, item in enumerate(items, start=1):
        macros, unmatched, from_recipe = await _macros_for(session, item)
        fi = FoodItem(
            entry_id=entry.id,
            position=position,
            food_id=item.resolution.food_id if item.resolution else None,
            recipe_id=item.resolution.recipe_id if item.resolution else None,
            detected_name=item.detected_name,
            detected_state=item.detected_state,
            grams=item.grams,
            grams_source=item.grams_source,
            grams_confidence=item.grams_confidence,
            resolution_method=(item.resolution.method if item.resolution else ResolutionMethod.new),
            resolution_confidence=item.resolution.confidence if item.resolution else 0.0,
            kcal=macros.kcal,
            protein_g=macros.protein_g,
            carbs_g=macros.carbs_g,
            fat_g=macros.fat_g,
            fiber_g=macros.fiber_g,
        )
        session.add(fi)
        logged.append(
            LoggedItem(
                name=item.detected_name,
                grams=item.grams,
                kcal=macros.kcal,
                protein_g=macros.protein_g,
                unmatched=unmatched,
                from_recipe=from_recipe,
            )
        )

    await session.flush()

    if entry_id_to_supersede is not None:
        old = await session.get(LogEntry, entry_id_to_supersede)
        if old is not None and old.superseded_by is None:
            old.superseded_by = entry.id

    return LoggedMeal(entry=entry, items=logged)


def macros_for_food(food: Food, *, grams: float, detected_state: FoodState) -> compute_mod.Macros:
    """Macros for `grams` of `food`, given the state it was observed in.

    The raw-versus-cooked and absorbed-oil decisions live here, in one function,
    because both need the row's state and the observed state together and
    getting either wrong silently is a large error in a known direction. Called
    from the write path and from the enrichment backfill, so a row researched
    later is priced exactly as it would have been priced at log time.
    """
    from_raw = (
        food.state == FoodState.raw
        and detected_state not in (FoodState.raw, FoodState.unknown)
        and bool(food.yield_factor)
    )
    # Deep frying adds 5-15% of the food's weight in oil, which appears in no
    # ingredient list. Applied only when the row describes the *unfried* food:
    # a row that is already "chips, fried" has the oil in its per-100g figures
    # and adding more would double count.
    absorb = (
        detected_state is FoodState.fried
        and food.state is not FoodState.fried
        and bool(food.fat_absorption_pct)
    )
    return compute_mod.compute(
        food,
        grams=grams,
        from_raw_row_but_eaten_cooked=from_raw,
        apply_fat_absorption=absorb,
    ).macros


async def _macros_for(
    session: AsyncSession, item: ItemToLog
) -> tuple[compute_mod.Macros, bool, bool]:
    """Macros for one item. Returns (macros, unmatched, from_recipe).

    The raw-versus-cooked rule lives here: a raw-ingredient row priced against
    a cooked portion must be divided by its yield factor first. A row that
    already describes the cooked or fried food is used as-is, since its
    per-100g figures already include cooking water or absorbed fat.
    """
    resolution = item.resolution

    if resolution is not None and resolution.recipe_id is not None:
        from umai.db.models import Recipe

        recipe = await session.get(Recipe, resolution.recipe_id)
        if recipe is not None and recipe.kcal_per_100g is not None:
            profile = RecipeProfile(
                kcal_per_100g=recipe.kcal_per_100g,
                protein_g_per_100g=recipe.protein_g_per_100g or 0.0,
                carbs_g_per_100g=recipe.carbs_g_per_100g or 0.0,
                fat_g_per_100g=recipe.fat_g_per_100g or 0.0,
                fiber_g_per_100g=None,
                canonical_name_en=recipe.name,
            )
            return compute_mod.serving(profile, item.grams).macros, False, True

    if resolution is None or resolution.food_id is None:
        # Unmatched, Phase 1 policy: log it with zero macros and flag it in the
        # reply. Inventing nutrition is a tier-4 row and arrives with the
        # label-photo path, never silently here.
        return compute_mod.ZERO, True, False

    food = await session.get(Food, resolution.food_id)
    if food is None:
        return compute_mod.ZERO, True, False

    return (
        macros_for_food(food, grams=item.grams, detected_state=item.detected_state),
        False,
        False,
    )


# ---------------------------------------------------------------------------
# The personal learning layer
# ---------------------------------------------------------------------------


async def remember(session: AsyncSession, user_id: uuid.UUID, meal: LoggedMeal) -> None:
    """Record what was logged into the library and the portion priors.

    Both tables were read by the resolver and the prompt builder and written by
    nothing, which meant resolution tier 2 was permanently inert and "the
    mechanism by which the system's largest error shrinks with use" shrank
    nothing. This is that mechanism.

    Called after the entry is written, on the same transaction. Cheap: two
    upserts per matched item against tables with one row per food you eat.
    """
    items = (
        (await session.execute(select(FoodItem).where(FoodItem.entry_id == meal.entry.id)))
        .scalars()
        .all()
    )
    now = meal.entry.occurred_at

    for item in items:
        if item.food_id is None:
            continue
        # The name the user's own logs use, which is what the library matches
        # against next time — not the canonical table name.
        display = (item.detected_name or "").strip()[:200] or "food"
        await session.execute(insert_library(user_id, item.food_id, display, now))
        await _update_prior(session, user_id, item.food_id)


def insert_library(
    user_id: uuid.UUID, food_id: uuid.UUID, display: str, now: dt.datetime
) -> Insert:
    """Upsert one library row, incrementing the count.

    `times_logged` is what breaks a near-tie in the resolver towards the food
    you eat weekly rather than the one you logged once in March, so it has to
    increment on conflict rather than being left at its insert value.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from umai.db.models import FoodLibrary

    return (
        pg_insert(FoodLibrary)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            food_id=food_id,
            display_name=display,
            times_logged=1,
            last_logged_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_library_user_food",
            set_={
                "times_logged": FoodLibrary.__table__.c.times_logged + 1,
                "last_logged_at": now,
                "display_name": display,
            },
        )
    )


# A prior computed from one or two servings is noise dressed as a hint, and it
# reaches the perception prompt where it biases the next estimate. Three is the
# smallest number at which a median means anything.
MIN_OBSERVATIONS_FOR_PRIOR = 3


async def _update_prior(session: AsyncSession, user_id: uuid.UUID, food_id: uuid.UUID) -> None:
    """Recompute this food's portion prior from the user's own history.

    Percentiles in SQL rather than in Python: the whole point is the median of
    *your* servings, the database already holds them, and a correction the user
    made by hand is worth more than any estimate — which is why corrected items
    are counted the same as any other, having already replaced what they
    corrected via the supersede chain.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from umai.db.models import PortionPrior

    stats = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(FoodItem.grams).label("median"),
                func.percentile_cont(0.25).within_group(FoodItem.grams).label("p25"),
                func.percentile_cont(0.75).within_group(FoodItem.grams).label("p75"),
                func.count().label("n"),
            )
            .join(LogEntry, FoodItem.entry_id == LogEntry.id)
            .where(
                FoodItem.food_id == food_id,
                FoodItem.grams > 0,
                LogEntry.user_id == user_id,
                LogEntry.superseded_by.is_(None),
            )
        )
    ).one()

    if stats.n < MIN_OBSERVATIONS_FOR_PRIOR or stats.median is None:
        return

    await session.execute(
        pg_insert(PortionPrior)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            food_id=food_id,
            median_grams=float(stats.median),
            p25_grams=float(stats.p25) if stats.p25 is not None else None,
            p75_grams=float(stats.p75) if stats.p75 is not None else None,
            n_observations=int(stats.n),
        )
        .on_conflict_do_update(
            constraint="uq_prior_user_food",
            set_={
                "median_grams": float(stats.median),
                "p25_grams": float(stats.p25) if stats.p25 is not None else None,
                "p75_grams": float(stats.p75) if stats.p75 is not None else None,
                "n_observations": int(stats.n),
                "updated_at": func.now(),
            },
        )
    )


async def portion_priors(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 12
) -> dict[str, tuple[float, float, float]]:
    """The priors worth putting in a perception prompt, most-logged first.

    Keyed by the name the user's own logs use, because that is the name the
    model will produce again. Capped: a prompt listing forty foods is a prompt
    the model skims.
    """
    from umai.db.models import FoodLibrary, PortionPrior

    rows = (
        await session.execute(
            select(
                FoodLibrary.display_name,
                PortionPrior.median_grams,
                PortionPrior.p25_grams,
                PortionPrior.p75_grams,
            )
            .join(
                FoodLibrary,
                (FoodLibrary.food_id == PortionPrior.food_id)
                & (FoodLibrary.user_id == PortionPrior.user_id),
            )
            .where(PortionPrior.user_id == user_id)
            .order_by(FoodLibrary.times_logged.desc())
            .limit(limit)
        )
    ).all()

    return {
        r.display_name: (
            float(r.median_grams),
            float(r.p25_grams if r.p25_grams is not None else r.median_grams),
            float(r.p75_grams if r.p75_grams is not None else r.median_grams),
        )
        for r in rows
    }


# ---------------------------------------------------------------------------
# Corrections: immutable, traceable
# ---------------------------------------------------------------------------


async def supersede_with_grams(
    session: AsyncSession,
    entry_id: uuid.UUID,
    new_grams_by_item: dict[uuid.UUID, float],
) -> LoggedMeal | None:
    """Replace an entry with corrected grams. The original row is never
    mutated; it is pointed at its replacement and a Correction row records the
    change, which is what keeps the audit trail honest."""
    old = (
        await session.execute(
            select(LogEntry).options(selectinload(LogEntry.items)).where(LogEntry.id == entry_id)
        )
    ).scalar_one_or_none()
    if old is None or old.superseded_by is not None:
        return None
    # Position order is the order the user was shown; UUID keys do not
    # preserve it, so it is applied explicitly rather than trusted.
    old.items.sort(key=lambda i: (i.position is None, i.position))

    items: list[ItemToLog] = []
    for fi in old.items:
        corrected = fi.id in new_grams_by_item
        grams = new_grams_by_item.get(fi.id, fi.grams)
        # Setting an item to zero grams means "that is not mine", not "I ate
        # none of it": carrying a 0g phantom forward leaves a row that shows up
        # in every later listing and teaches the portion priors nothing.
        if corrected and grams <= 0:
            continue
        resolution = Resolution(
            food_id=fi.food_id,
            recipe_id=fi.recipe_id,
            display_name=fi.detected_name or "",
            method=fi.resolution_method,
            confidence=fi.resolution_confidence or 0.0,
        )
        items.append(
            ItemToLog(
                detected_name=fi.detected_name or "food",
                detected_state=fi.detected_state or FoodState.unknown,
                grams=grams,
                # Provenance is per item. Stamping every item as user-weighed
                # because one of them was is a fabricated provenance, and the
                # calibration engine is entitled to weight a user-stated gram
                # value above a model-estimated one.
                grams_source=GramsSource.user if corrected else fi.grams_source,
                grams_confidence=1.0 if corrected else fi.grams_confidence,
                resolution=resolution,
            )
        )

    meal = await log_food_items(
        session,
        old.user_id,
        items,
        occurred_at=old.occurred_at,
        source=old.source,
        note=old.note,
        entry_id_to_supersede=old.id,
    )

    for fi in old.items:
        if fi.id in new_grams_by_item:
            session.add(
                Correction(
                    entry_id=old.id,
                    food_item_id=fi.id,
                    field="grams",
                    old_value=str(fi.grams),
                    new_value=str(new_grams_by_item[fi.id]),
                )
            )
    await session.flush()
    return meal


# ---------------------------------------------------------------------------
# Editing and removing today's entries
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TodayEntry:
    """One live entry from today, shaped for the edit list.

    The local time string is computed once here, where the user's timezone is
    at hand, so the keyboard layer never touches time at all.
    """

    entry_id: uuid.UUID
    kind: EntryKind
    occurred_at: dt.datetime
    kcal: float
    ml: float
    item_count: int
    local_time: str

    @property
    def prefix(self) -> str:
        return str(self.entry_id)[:8]

    @property
    def button_label(self) -> str:
        if self.kind is EntryKind.water:
            return f"{self.local_time} · 💧 {self.ml:.0f} ml"
        return f"{self.local_time} · {self.kcal:.0f} kcal"


async def today_entries(session: AsyncSession, user: User, clock: Clock) -> list[TodayEntry]:
    """Today's live entries, oldest first, meals and water together.

    The edit list is the one place a user goes to fix a mistake, so it shows
    everything that can be fixed: a mis-tapped water button is exactly as
    wrong as a mis-estimated meal.
    """
    start, end = day_bounds(today(clock, user.tz), user.tz)
    rows = (
        (
            await session.execute(
                select(LogEntry)
                .where(
                    LogEntry.user_id == user.id,
                    LogEntry.occurred_at >= start,
                    LogEntry.occurred_at < end,
                    LogEntry.superseded_by.is_(None),
                    LogEntry.kind.in_((EntryKind.food, EntryKind.drink, EntryKind.water)),
                )
                .order_by(LogEntry.occurred_at)
            )
        )
        .scalars()
        .all()
    )

    out: list[TodayEntry] = []
    for e in rows:
        local = e.occurred_at.astimezone(ZoneInfo(user.tz)).strftime("%H:%M")
        if e.kind is EntryKind.water:
            out.append(
                TodayEntry(
                    entry_id=e.id,
                    kind=e.kind,
                    occurred_at=e.occurred_at,
                    kcal=0.0,
                    ml=float(e.value or 0.0),
                    item_count=0,
                    local_time=local,
                )
            )
        else:
            kcal, n_items = (
                await session.execute(
                    select(
                        func.coalesce(func.sum(FoodItem.kcal), 0.0),
                        func.count(FoodItem.id),
                    ).where(FoodItem.entry_id == e.id)
                )
            ).one()
            out.append(
                TodayEntry(
                    entry_id=e.id,
                    kind=e.kind,
                    occurred_at=e.occurred_at,
                    kcal=float(kcal),
                    ml=0.0,
                    item_count=int(n_items),
                    local_time=local,
                )
            )
    return out


async def hard_delete_entry(session: AsyncSession, user_id: uuid.UUID, entry_id: uuid.UUID) -> bool:
    """Remove an entry and everything it dragged in, for good.

    The user asked for removal, not archiving, so the row goes. What must not
    go with it:

      * The photo. `media` cascades on the entry FK, but the media row is the
        re-scoring archive: the raw model response in `perception_runs` hangs
        off it and exists precisely so an old photo can be re-scored when a
        better model arrives. It is detached, not deleted.
      * The chain. A corrected meal is a supersede chain, newest live link
        last. Deleting only the newest link would resurrect the version the
        user corrected, and deleting a meal they asked removed must remove the
        meal they ate, not the first draft of it.
      * The corrections rows. They reference the entry and its items with no
        ON DELETE clause, so they are cleared first; an audit of a deleted
        thing is noise.

    Returns False when the entry is not the user's or already superseded
    (an inner chain link is never addressable from the UI; deleting it would
    corrupt the chain of the live entry).
    """
    entry = await session.get(LogEntry, entry_id)
    if entry is None or entry.user_id != user_id or entry.superseded_by is not None:
        return False

    # Walk the chain back from the live entry to the original.
    chain = [entry_id]
    while True:
        predecessor = (
            await session.execute(select(LogEntry.id).where(LogEntry.superseded_by == chain[-1]))
        ).scalar_one_or_none()
        if predecessor is None:
            break
        chain.append(predecessor)

    for eid in chain:
        await session.execute(
            delete(Correction).where(
                or_(
                    Correction.entry_id == eid,
                    Correction.food_item_id.in_(
                        select(FoodItem.id).where(FoodItem.entry_id == eid)
                    ),
                )
            )
        )
        await session.execute(update(Media).where(Media.entry_id == eid).values(entry_id=None))
        # An inner link of someone else's chain cannot exist here (each entry
        # has at most one successor), but the SET NULL keeps the delete legal
        # regardless of who points where.
        await session.execute(
            update(LogEntry).where(LogEntry.superseded_by == eid).values(superseded_by=None)
        )

    for eid in chain:
        # food_items cascade on the entry FK; corrections and media were
        # handled above.
        await session.execute(delete(LogEntry).where(LogEntry.id == eid))
    return True


async def edit_water(
    session: AsyncSession,
    user_id: uuid.UUID,
    entry_id: uuid.UUID,
    new_ml: float,
) -> LogEntry | None:
    """Replace a water entry's amount. Old row out, new row in.

    Consistent with hard delete: the user chose removal over archiving, so
    editing is not modelled as a supersede. The original timestamp is kept,
    because when you drank the water did not change, only the number did.
    """
    old = await session.get(LogEntry, entry_id)
    if (
        old is None
        or old.user_id != user_id
        or old.kind is not EntryKind.water
        or old.superseded_by is not None
    ):
        return None
    occurred_at, source = old.occurred_at, old.source
    if not await hard_delete_entry(session, user_id, entry_id):
        return None
    return await log_simple(
        session,
        user_id,
        kind=EntryKind.water,
        value=new_ml,
        unit="ml",
        occurred_at=occurred_at,
        source=source,
    )


# ---------------------------------------------------------------------------
# Simple logs: water, weight, supplements
# ---------------------------------------------------------------------------


_SIMPLE_KINDS = (
    EntryKind.water,
    EntryKind.weight,
    EntryKind.supplement,
    EntryKind.mood,
    EntryKind.satiety,
)


async def log_simple(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    kind: EntryKind,
    value: float,
    unit: str | None = None,
    occurred_at: dt.datetime,
    source: EntrySource = EntrySource.button,
) -> LogEntry:
    if kind not in _SIMPLE_KINDS:
        raise ValueError(f"log_simple is for value-carrying kinds, not {kind}")
    if kind == EntryKind.weight and not safety.is_plausible_weight(value):
        raise ValueError(f"{value} kg is not a plausible body weight")
    entry = LogEntry(
        user_id=user_id,
        occurred_at=occurred_at,
        kind=kind,
        source=source,
        value=value,
        unit=unit,
    )
    session.add(entry)
    await session.flush()
    return entry


# ---------------------------------------------------------------------------
# The daily read path
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DayTotals:
    kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    water_ml: float
    entry_count: int
    unmatched_items: int


async def day_totals(
    session: AsyncSession, user: User, clock: Clock, day: dt.date | None = None
) -> DayTotals:
    """What has been logged so far today (or on `day`), superseded entries excluded.

    This is the number every summary and budget statement is built from, so it
    is one query in one place rather than three slightly different sums in
    three handlers.
    """
    start, end = day_bounds(day or today(clock, user.tz), user.tz)
    window = (LogEntry.occurred_at >= start, LogEntry.occurred_at < end)

    macros = (
        select(
            func.coalesce(func.sum(FoodItem.kcal), 0.0),
            func.coalesce(func.sum(FoodItem.protein_g), 0.0),
            func.coalesce(func.sum(FoodItem.carbs_g), 0.0),
            func.coalesce(func.sum(FoodItem.fat_g), 0.0),
            func.coalesce(func.sum(case((FoodItem.food_id.is_(None), 1), else_=0)), 0),
        )
        .join(LogEntry, FoodItem.entry_id == LogEntry.id)
        .where(
            LogEntry.user_id == user.id,
            *window,
            LogEntry.superseded_by.is_(None),
            LogEntry.kind.in_((EntryKind.food, EntryKind.drink)),
        )
    )
    kcal, protein, carbs, fat, unmatched = (await session.execute(macros)).one()

    simple = select(
        func.coalesce(func.sum(case((LogEntry.kind == EntryKind.water, LogEntry.value))), 0.0),
        func.count(LogEntry.id),
    ).where(
        LogEntry.user_id == user.id,
        *window,
        LogEntry.superseded_by.is_(None),
    )
    water, count = (await session.execute(simple)).one()

    return DayTotals(
        kcal=float(kcal),
        protein_g=float(protein),
        carbs_g=float(carbs),
        fat_g=float(fat),
        water_ml=float(water),
        entry_count=int(count),
        unmatched_items=int(unmatched),
    )


async def latest_weight(session: AsyncSession, user_id: uuid.UUID) -> float | None:
    """Most recent weight, whether it arrived by button or by health sync."""
    return (await _weight_rows(session, user_id, 1))[0]


async def previous_weight(session: AsyncSession, user_id: uuid.UUID) -> float | None:
    """The reading before the latest one, for the ↓/↑ in the weigh-in reply."""
    rows = await _weight_rows(session, user_id, 2)
    return rows[1] if len(rows) > 1 else None


async def _weight_rows(session: AsyncSession, user_id: uuid.UUID, limit: int) -> list[float]:
    from umai.db.models import HealthMetric

    entry_w = (
        select(LogEntry.value, LogEntry.occurred_at)
        .where(
            LogEntry.user_id == user_id,
            LogEntry.kind == EntryKind.weight,
            LogEntry.superseded_by.is_(None),
        )
        .order_by(LogEntry.occurred_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(entry_w)).all()

    health_w = (
        select(HealthMetric.value, HealthMetric.recorded_at)
        .where(HealthMetric.user_id == user_id, HealthMetric.metric == "weight_kg")
        .order_by(HealthMetric.recorded_at.desc())
        .limit(limit)
    )
    hrows = (await session.execute(health_w)).all()

    merged: list[tuple[dt.datetime, float]] = [
        *((when, float(v)) for v, when in rows),
        *((when, float(v)) for v, when in hrows),
    ]
    merged.sort(key=lambda r: r[0], reverse=True)
    return [v for _, v in merged[:limit]]


def current_target(user: User, weight_kg: float, clock: Clock) -> safety.TargetDecision:
    """The Phase 1 static target: Mifflin-St Jeor with the safety floors.

    Calibration-derived targets replace this in Phase 3; until then every
    number shown to the user passes through decide_target, which is where the
    floors live in code rather than in a prompt.
    """
    if user.sex not in ("male", "female") or not user.height_cm or not user.birth_date:
        raise RuntimeError(
            "person fields (sex, height, birth date) are unset; they seed BMR "
            "and the safety floors. See UMAI_SEX / UMAI_HEIGHT_CM / UMAI_BIRTH_DATE."
        )
    age = safety.age_years(user.birth_date, today(clock, user.tz))
    sex: Literal["male", "female"] = "male" if user.sex == "male" else "female"
    return safety.decide_target(
        sex=sex,
        weight_kg=weight_kg,
        height_cm=user.height_cm,
        age=age,
        tdee_estimate=safety.mifflin_st_jeor(
            sex=sex,
            weight_kg=weight_kg,
            height_cm=user.height_cm,
            age_years=age,
        )
        * 1.4,  # sedentary-to-light multiplier; replaced by the fit later
        goal_rate_kg_per_week=user.goal_rate_kg_per_week or -0.5,
    )


def format_meal(meal: LoggedMeal) -> str:
    """Stage 4 of the pipeline: one message, items and grams, not a wall of
    macros. The only correction usually needed is grams, so grams are what is
    shown.

    When some items are unmatched the total is deliberately *not* presented as
    a total. It is arithmetically correct and factually absurd, the first live
    session showed a plate of lahmacun as "Total 10 kcal", and a number a user
    reads as their day's intake must not be silently missing most of the plate.
    The enrichment job fills these in within minutes; the wording says so.
    """
    lines = []
    for n, item in enumerate(meal.items, start=1):
        if item.unmatched:
            lines.append(f"{n}. {item.name}: {item.grams:.0f}g, still looking it up ⏳")
        else:
            lines.append(f"{n}. {item.name}: {item.grams:.0f}g, {item.kcal:.0f} kcal")

    missing = sum(1 for i in meal.items if i.unmatched)
    if not missing:
        lines.append(f"Total {meal.total_kcal:.0f} kcal")
    elif missing == len(meal.items):
        lines.append("No totals yet. None of these are in the food table.")
    else:
        lines.append(f"At least {meal.total_kcal:.0f} kcal. Still looking up {missing} item(s).")
    return "\n".join(lines)


def format_entry(user: User, entry: LogEntry) -> str:
    """Re-render a logged entry for the edit view.

    The meal exactly as it was first shown (so the ✏️ item numbers line up
    with what the user remembers), or the water amount.
    """
    local = entry.occurred_at.astimezone(ZoneInfo(user.tz)).strftime("%H:%M")
    if entry.kind is EntryKind.water:
        return f"💧 {float(entry.value or 0.0):.0f} ml at {local}"
    items = sorted(entry.items, key=lambda i: (i.position is None, i.position))
    return format_meal(
        LoggedMeal(
            entry=entry,
            items=[
                LoggedItem(
                    name=fi.detected_name or "food",
                    grams=fi.grams,
                    kcal=fi.kcal,
                    protein_g=fi.protein_g,
                    unmatched=fi.food_id is None and fi.recipe_id is None,
                )
                for fi in items
            ],
        )
    )


def format_day(user: User, totals: DayTotals, target: safety.TargetDecision | None) -> str:
    """The one summary formatter. Deterministic, in code, no model call."""
    lines = [
        f"Today: {totals.kcal:.0f} kcal"
        + (f" of {target.kcal_target}" if target else "")
        + f", protein {totals.protein_g:.0f}g"
    ]
    if target:
        left = target.kcal_target - totals.kcal
        if left >= 0:
            lines.append(f"{left:.0f} kcal left, protein target {target.protein_target_g}g")
        else:
            lines.append(f"{-left:.0f} kcal over, protein target {target.protein_target_g}g")
    if totals.water_ml:
        lines.append(f"Water {totals.water_ml:.0f} ml")
    if totals.unmatched_items:
        lines.append(
            f"⏳ {totals.unmatched_items} item(s) not yet in the food table, so "
            "this total may rise."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def get_or_create_user(
    session: AsyncSession, settings: Settings, telegram_id: int, *, clock: Clock
) -> User:
    """Single-user auto-registration, person fields seeded from settings.

    A plausible-looking wrong default for sex or height would poison BMR and
    the floors, so those stay empty (and targets refuse to compute) until the
    environment provides them.
    """
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if user is not None:
        return user

    user = User(
        telegram_id=telegram_id,
        tz=settings.tz,
        sex=settings.sex or None,
        height_cm=settings.height_cm,
        birth_date=settings.birth_date,
        goal_rate_kg_per_week=settings.goal_rate_kg_per_week,
        # A seed, not a setting. /cuisines is the source of truth from here on,
        # and an empty list is a perfectly good starting point.
        cuisines=settings.cuisine_list,
    )
    session.add(user)
    await session.flush()

    if settings.start_weight_kg is not None:
        # The weight series starts at onboarding, not at the first manual
        # weigh-in, so trend and calibration have a baseline from day one.
        await log_simple(
            session,
            user.id,
            kind=EntryKind.weight,
            value=settings.start_weight_kg,
            unit="kg",
            occurred_at=clock.now(),
            source=EntrySource.manual,
        )
    return user


# ---------------------------------------------------------------------------
# The service bundle handed to handlers and jobs
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Services:
    """Everything a handler needs, so no handler reaches for a global."""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
