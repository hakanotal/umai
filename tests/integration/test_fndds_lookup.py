"""The FNDDS lookup, against a real database.

Two things need Postgres and cannot be faked: pg_trgm's similarity, which is
what decides whether a search hits, and the enrichment loop's insistence that a
copied fdc_id was really returned by a real query.

These assertions are written to hold whether or not the full 5,431-row table
has been seeded, because the dev database usually has been. So they either
assert on a row this test inserted under a name nothing else uses, or they
assert an *absence* — which extra rows cannot create.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json

import pytest
from sqlalchemy import select

from umai.clock import FakeClock
from umai.core import enrichment, fndds, tools
from umai.db.models import FnddsFood, Food, FoodItem, FoodSource, FoodState, ResolutionMethod
from umai.resolver.match import Resolution

CLOCK = FakeClock(dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC))

# Deliberately unlike anything USDA publishes, so the assertions do not depend
# on whether the real table has been seeded into this database.
QUARGLE = 9_900_001
FLIMBREAD = 9_900_002


async def seed_rows(session) -> None:
    session.add_all(
        [
            FnddsFood(
                fdc_id=QUARGLE,
                food_code="99000010",
                description="Quargleflop, stewed",
                wweia_category="Test foods",
                kcal_per_100g=120.0,
                protein_g_per_100g=8.0,
                carbs_g_per_100g=12.0,
                fat_g_per_100g=4.0,
                fiber_g_per_100g=1.0,
                sugar_g_per_100g=None,
                sodium_mg_per_100g=None,
            ),
            FnddsFood(
                fdc_id=FLIMBREAD,
                food_code="99000020",
                description="Flimbread, plain",
                wweia_category="Test foods",
                kcal_per_100g=280.0,
                protein_g_per_100g=9.0,
                carbs_g_per_100g=53.0,
                fat_g_per_100g=3.0,
                fiber_g_per_100g=2.0,
                sugar_g_per_100g=None,
                sodium_mg_per_100g=None,
            ),
        ]
    )
    await session.flush()


# --- the search ------------------------------------------------------------


async def test_a_name_in_the_table_is_found_with_its_numbers(session):
    await seed_rows(session)
    hits = await fndds.search(session, "quargleflop stewed")
    assert hits[0].fdc_id == QUARGLE
    assert hits[0].kcal_per_100g == 120.0
    assert hits[0].score > fndds.MIN_SCORE


@pytest.mark.parametrize(
    "turkish", ["lahmacun", "doner kebab", "ayran", "simit", "menemen", "mercimek corbasi"]
)
async def test_a_turkish_dish_name_returns_nothing_rather_than_noise(session, turkish):
    """The boundary of what this table is for, enforced by the threshold.

    Without the floor these return Lard for lahmacun and Kefir for doner kebab.
    An empty result is the honest answer and the one the prompt tells the model
    to act on by searching ingredients instead. Extra rows in the table cannot
    make this pass spuriously — only fail it — so it is a real guard.
    """
    assert await fndds.search(session, turkish) == []


async def test_the_threshold_is_what_suppresses_a_weak_match(session):
    """The emptiness above is the floor at work, not an unrelated miss.

    The weak match is seeded here rather than assumed. "Lamb, cured, ham"
    scores about 0.09 against "lahmacun" — far under the default floor, well
    over a hand-set 0.01 — so the pair of assertions brackets the threshold
    using only rows this test inserted. Relying on the real FNDDS table to
    supply a weak match, as this test used to, made it pass on a seeded dev
    database and fail on an empty one.
    """
    await seed_rows(session)
    session.add(
        FnddsFood(
            fdc_id=9_900_003,
            food_code="99000030",
            description="Lamb, cured, ham",
            wweia_category="Test foods",
            kcal_per_100g=200.0,
            protein_g_per_100g=20.0,
            carbs_g_per_100g=0.0,
            fat_g_per_100g=14.0,
            fiber_g_per_100g=0.0,
            sugar_g_per_100g=None,
            sodium_mg_per_100g=None,
        )
    )
    await session.flush()

    assert await fndds.search(session, "lahmacun") == []
    loose = await fndds.search(session, "lahmacun", min_score=0.01)
    assert loose, "with the floor removed, the weak match seeded above is found"


async def test_an_empty_query_is_not_sent_to_the_database(session):
    assert await fndds.search(session, "   ") == []


async def test_by_ids_returns_only_rows_that_exist(session):
    await seed_rows(session)
    rows = await fndds.by_ids(session, [QUARGLE, 9_999_999])
    assert set(rows) == {QUARGLE}


# --- the loop --------------------------------------------------------------


class FakeModel:
    """A ModelClient stand-in that replays a scripted tool conversation.

    `research` only ever calls `acall_tools`, so this is the whole surface.
    """

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self.queries: list[str] = []

    async def acall_tools(self, task, *, messages, tools, schema=None):
        step = self._script.pop(0)
        for message in messages:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                for call in message["tool_calls"]:
                    query = json.loads(call["function"]["arguments"])["query"]
                    if query not in self.queries:
                        self.queries.append(query)
        return _Message(step), "fake/model", 1


class _Message:
    def __init__(self, step: dict):
        self.content = step.get("content")
        calls = step.get("tool_calls") or []
        self.tool_calls = [_Call(i, c) for i, c in enumerate(calls)] or None


class _Call:
    def __init__(self, i: int, query: str):
        self.id = f"call_{i}"
        self.function = _Fn(query)


class _Fn:
    name = "fndds_search"

    def __init__(self, query: str):
        self.arguments = json.dumps({"query": query})


def factory_for(session):
    """A session_factory that hands back the test's own transaction.

    The real one opens a fresh session per search; here every search must see
    the rows this test inserted but never committed.
    """

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return lambda: _Ctx()


async def test_a_copied_row_becomes_a_tier_2_food(session):
    await seed_rows(session)
    model = FakeModel(
        [
            {
                "content": json.dumps(
                    {
                        "is_food": True,
                        "method": "copy",
                        "fdc_id": QUARGLE,
                        "canonical_name_en": "quargleflop",
                        "aliases": ["kuargleflop"],
                        "state": "boiled",
                        "confidence": 0.9,
                        "basis": "the stewed row is the dish",
                    }
                )
            }
        ]
    )
    gap = enrichment.Gap("quargleflop stewed", FoodState.boiled, 1)
    candidate, served = await enrichment.research(model, gap, [], factory_for(session))

    assert served == "fake/model"
    assert candidate.trust_tier == 2
    assert candidate.source is FoodSource.usda_sr
    assert candidate.kcal_per_100g == 120.0
    assert str(QUARGLE) in candidate.source_ref


async def test_the_first_search_is_run_in_code_before_the_model_is_asked(session):
    """One free query on the item's own name, so an English name costs one turn.

    The model here answers immediately, without ever calling the tool, and can
    still copy the row — which is only possible if the seeded search put it in
    front of it and registered it as offered.
    """
    await seed_rows(session)
    model = FakeModel(
        [
            {
                "content": json.dumps(
                    {
                        "is_food": True,
                        "method": "copy",
                        "fdc_id": FLIMBREAD,
                        "canonical_name_en": "flimbread",
                        "aliases": [],
                        "state": "baked",
                        "confidence": 0.9,
                        "basis": "exact",
                    }
                )
            }
        ]
    )
    gap = enrichment.Gap("flimbread plain", FoodState.baked, 1)
    candidate, _ = await enrichment.research(model, gap, [], factory_for(session))
    assert candidate.kcal_per_100g == 280.0
    assert model.queries == [], "the model needed no tool call of its own"


async def test_ingredients_are_searched_one_at_a_time_and_composed(session):
    await seed_rows(session)
    model = FakeModel(
        [
            {"tool_calls": ["quargleflop stewed"]},
            {"tool_calls": ["flimbread plain"]},
            {
                "content": json.dumps(
                    {
                        "is_food": True,
                        "method": "compose",
                        "components": [
                            {"fdc_id": FLIMBREAD, "pct": 50},
                            {"fdc_id": QUARGLE, "pct": 50},
                        ],
                        "canonical_name_en": "flimbread with quargleflop",
                        "aliases": [],
                        "state": "baked",
                        "confidence": 0.7,
                        "basis": "half and half",
                    }
                )
            },
        ]
    )
    gap = enrichment.Gap("flimbread with quargleflop", FoodState.baked, 1)
    candidate, _ = await enrichment.research(model, gap, [], factory_for(session))

    assert model.queries == ["quargleflop stewed", "flimbread plain"]
    assert candidate.trust_tier == 4
    assert candidate.kcal_per_100g == pytest.approx(200.0)  # (280 + 120) / 2


async def test_a_model_that_never_stops_searching_is_given_up_on(session):
    await seed_rows(session)
    model = FakeModel([{"tool_calls": ["x"]} for _ in range(enrichment.MAX_TOOL_TURNS)])
    gap = enrichment.Gap("something", FoodState.unknown, 1)
    with pytest.raises(enrichment.Rejected, match="no answer after"):
        await enrichment.research(model, gap, [], factory_for(session))


# --- what the row does once written ----------------------------------------


async def test_a_tier_2_row_upgrades_the_tier_4_guess_it_replaces(session):
    """A dish researched before the table was seeded must not stay guessed."""
    guessed = enrichment.Candidate(
        canonical_name_en="quargleflop",
        aliases=["old alias"],
        state=FoodState.boiled,
        kcal_per_100g=999.0,
        protein_g_per_100g=1.0,
        carbs_g_per_100g=1.0,
        fat_g_per_100g=1.0,
        fiber_g_per_100g=None,
        density_g_per_ml=None,
        yield_factor=None,
        fat_absorption_pct=None,
        confidence=0.5,
        basis="a guess",
    )
    await enrichment.upsert_food(session, guessed)
    await session.flush()

    measured = dataclasses.replace(
        guessed,
        aliases=["kuargleflop"],
        kcal_per_100g=120.0,
        method="copy",
        source=FoodSource.usda_sr,
        source_ref=f"fndds:{QUARGLE} Quargleflop, stewed",
        trust_tier=2,
    )
    food = await enrichment.upsert_food(session, measured)
    await session.flush()

    assert food.trust_tier == 2
    assert food.kcal_per_100g == 120.0
    # The old alias is kept: the resolver may have been matching on it.
    assert set(food.aliases) == {"old alias", "kuargleflop"}


async def test_a_tier_1_row_is_never_overwritten(session):
    """The rule this upgrade must not break."""
    session.add(
        Food(
            canonical_name_en="quargleflop",
            aliases=[],
            state=FoodState.boiled,
            kcal_per_100g=111.0,
            protein_g_per_100g=1.0,
            carbs_g_per_100g=1.0,
            fat_g_per_100g=1.0,
            source=FoodSource.usda_foundation,
            source_ref="lab",
            trust_tier=1,
        )
    )
    await session.flush()

    incoming = enrichment.Candidate(
        canonical_name_en="quargleflop",
        aliases=[],
        state=FoodState.boiled,
        kcal_per_100g=120.0,
        protein_g_per_100g=8.0,
        carbs_g_per_100g=12.0,
        fat_g_per_100g=4.0,
        fiber_g_per_100g=None,
        density_g_per_ml=None,
        yield_factor=None,
        fat_absorption_pct=None,
        confidence=0.9,
        basis="fndds",
        method="copy",
        source=FoodSource.usda_sr,
        source_ref="fndds:1",
        trust_tier=2,
    )
    food = await enrichment.upsert_food(session, incoming)
    assert food.trust_tier == 1
    assert food.kcal_per_100g == 111.0


async def test_backfill_trusts_a_measured_row_more_than_a_guessed_one(session, user):
    item = tools.ItemToLog(
        detected_name="quargleflop",
        detected_state=FoodState.boiled,
        grams=200.0,
        grams_source="vlm",
        resolution=Resolution(
            food_id=None,
            recipe_id=None,
            display_name="quargleflop",
            method=ResolutionMethod.new,
            confidence=0.0,
        ),
    )
    logged = await tools.log_food_items(
        session, user.id, [item], occurred_at=CLOCK.now(), source="photo"
    )
    await session.flush()

    measured = enrichment.Candidate(
        canonical_name_en="quargleflop",
        aliases=[],
        state=FoodState.boiled,
        kcal_per_100g=120.0,
        protein_g_per_100g=8.0,
        carbs_g_per_100g=12.0,
        fat_g_per_100g=4.0,
        fiber_g_per_100g=1.0,
        density_g_per_ml=None,
        yield_factor=None,
        fat_absorption_pct=None,
        confidence=0.9,
        basis="fndds",
        method="copy",
        source=FoodSource.usda_sr,
        source_ref=f"fndds:{QUARGLE}",
        trust_tier=2,
    )
    food = await enrichment.upsert_food(session, measured)
    n = await enrichment.backfill(session, enrichment.Gap("quargleflop", FoodState.boiled, 1), food)
    await session.flush()

    assert n == 1
    row = (
        (await session.execute(select(FoodItem).where(FoodItem.entry_id == logged.entry.id)))
        .scalars()
        .one()
    )
    assert row.food_id == food.id
    assert row.kcal == pytest.approx(240.0)  # 200g of a 120 kcal/100g row
    assert row.resolution_confidence == 0.7
