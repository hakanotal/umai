"""Stage 2, resolution.

Search order: recipe, personal library, canonical foods, then a provisional new
entry flagged unverified. The small model is a tiebreak only, invoked when the
top candidates are close or when nothing matches. It decides *which row*, never
*how much*.

Phase 1 matches names with pg_trgm rather than embeddings. With 200-300 foods
and a handful of recipes, trigram similarity against canonical_name_en plus the
aliases array resolves well, using an index, with no model download and no
embedding service on the critical path.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import Float, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from umai.config.models import ModelClient
from umai.db.models import Food, FoodLibrary, FoodSource, FoodState, Recipe, ResolutionMethod

log = logging.getLogger(__name__)

# Above this, take the top row and do not spend a model call.
CONFIDENT = 0.72
# Below this, nothing in the table is plausibly the same food.
FLOOR = 0.30
# If the top two are within this of each other, it is a genuine tie and worth
# asking the utility model which one it is.
TIE_MARGIN = 0.08

CANDIDATES = 5


@dataclass(slots=True)
class Candidate:
    food_id: uuid.UUID
    name: str
    state: FoodState
    score: float
    trust_tier: int


@dataclass(slots=True)
class Resolution:
    food_id: uuid.UUID | None
    recipe_id: uuid.UUID | None
    display_name: str
    method: ResolutionMethod
    confidence: float
    candidates: tuple[Candidate, ...] = ()
    provisional: bool = False


class Resolver:
    def __init__(self, session: AsyncSession, models: ModelClient | None = None) -> None:
        self._db = session
        self._models = models

    async def resolve(
        self,
        detected_name: str,
        detected_state: FoodState,
        user_id: uuid.UUID,
    ) -> Resolution:
        name = detected_name.strip().lower()

        recipe = await self._match_recipe(name, user_id)
        if recipe is not None:
            return recipe

        library = await self._match_library(name, user_id)
        if library is not None:
            return library

        candidates = await self._search_foods(name, detected_state)
        if not candidates:
            return Resolution(
                food_id=None,
                recipe_id=None,
                display_name=name,
                method=ResolutionMethod.new,
                confidence=0.0,
                provisional=True,
            )

        top = candidates[0]

        # An exact name-and-state hit needs no further thought.
        if top.score >= 0.999:
            return _from(top, ResolutionMethod.exact, candidates)

        runner_up = candidates[1].score if len(candidates) > 1 else 0.0
        clear_winner = (top.score - runner_up) > TIE_MARGIN

        if top.score >= CONFIDENT and clear_winner:
            return _from(top, ResolutionMethod.trigram, candidates)

        if top.score < FLOOR:
            return Resolution(
                food_id=None,
                recipe_id=None,
                display_name=name,
                method=ResolutionMethod.new,
                confidence=top.score,
                candidates=tuple(candidates),
                provisional=True,
            )

        picked = await self._tiebreak(name, detected_state, candidates)
        return picked or _from(top, ResolutionMethod.trigram, candidates)

    # --- a. recipe ---------------------------------------------------------

    async def _match_recipe(self, name: str, user_id: uuid.UUID) -> Resolution | None:
        """Is this your usual dish? A tier 3 hit beats any photo estimate.

        Phase 1 matches on name alone. Phase 2 adds the image embedding, which
        is what makes "your usual lentil soup?" work from the photo itself.
        """
        sim = func.similarity(func.lower(Recipe.name), name)
        stmt = (
            select(Recipe.id, Recipe.name, cast(sim, Float).label("score"))
            .where(Recipe.user_id == user_id, Recipe.kcal_per_100g.is_not(None))
            .order_by(sim.desc())
            .limit(1)
        )
        row = (await self._db.execute(stmt)).first()
        if row and row.score >= CONFIDENT:
            return Resolution(
                food_id=None,
                recipe_id=row.id,
                display_name=row.name,
                method=ResolutionMethod.recipe,
                confidence=float(row.score),
            )
        return None

    # --- b. personal library ----------------------------------------------

    async def _match_library(self, name: str, user_id: uuid.UUID) -> Resolution | None:
        """Have you logged this before? Weighted by how often, so a food you eat
        weekly wins a near-tie against one you logged once in March."""
        sim = func.similarity(func.lower(FoodLibrary.display_name), name)
        stmt = (
            select(
                FoodLibrary.food_id,
                FoodLibrary.display_name,
                cast(sim, Float).label("score"),
                FoodLibrary.times_logged,
            )
            .where(FoodLibrary.user_id == user_id)
            .order_by(sim.desc(), FoodLibrary.times_logged.desc())
            .limit(1)
        )
        row = (await self._db.execute(stmt)).first()
        if row and row.score >= CONFIDENT:
            return Resolution(
                food_id=row.food_id,
                recipe_id=None,
                display_name=row.display_name,
                method=ResolutionMethod.library,
                confidence=float(row.score),
            )
        return None

    # --- c. canonical foods ------------------------------------------------

    async def _search_foods(self, name: str, state: FoodState) -> list[Candidate]:
        """Trigram similarity on the canonical name, or on any alias.

        The alias array is what lets "mercimek corbasi", "lentil soup" and "red
        lentil soup" all reach one row while the chat still speaks either
        language.
        """
        name_sim = func.similarity(func.lower(Food.canonical_name_en), name)
        # Aliases are flattened rather than unnested: at a few hundred rows the
        # difference is unmeasurable, and a correlated per-element maximum makes
        # this query considerably harder to read for no gain.
        alias_sim = func.similarity(func.lower(func.array_to_string(Food.aliases, " ")), name)
        score = func.greatest(name_sim, alias_sim)

        stmt = (
            select(
                Food.id,
                Food.canonical_name_en,
                Food.state,
                Food.trust_tier,
                cast(score, Float).label("score"),
            )
            .where(
                or_(
                    name_sim > 0.1,
                    alias_sim > 0.1,
                    Food.canonical_name_en.ilike(f"%{name}%"),
                )
            )
            .order_by(score.desc(), Food.trust_tier.asc())
            .limit(CANDIDATES * 3)
        )
        rows = (await self._db.execute(stmt)).all()

        out: list[Candidate] = []
        for r in rows:
            adjusted = float(r.score)
            # A row in the state the model reported is a better answer than the
            # same food in another state, and the raw-versus-cooked distinction
            # is the largest hidden error source in any food database.
            if r.state == state:
                adjusted = min(1.0, adjusted + 0.15)
            elif state is not FoodState.unknown and r.state is not FoodState.unknown:
                adjusted *= 0.85
            out.append(
                Candidate(
                    food_id=r.id,
                    name=r.canonical_name_en,
                    state=r.state,
                    score=adjusted,
                    trust_tier=r.trust_tier,
                )
            )

        out.sort(key=lambda c: (-c.score, c.trust_tier))
        return out[:CANDIDATES]

    # --- the tiebreak ------------------------------------------------------

    async def _tiebreak(
        self, name: str, state: FoodState, candidates: list[Candidate]
    ) -> Resolution | None:
        """Ask the utility model which row this is. Judgement, not arithmetic.

        Deliberately narrow: it picks an index from a list it did not choose. It
        cannot invent a food, cannot return a weight, and cannot return
        nutrition, so the worst case is a wrong row rather than a wrong number
        from nowhere.
        """
        if self._models is None:
            return None

        listing = "\n".join(f"{i}. {c.name} ({c.state.value})" for i, c in enumerate(candidates))
        try:
            content, _served, _ms = await self._models.acall(
                "resolution",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You match a food name to one row of a food composition table. "
                            'Reply with JSON: {"index": <int>, "confidence": <0..1>}. '
                            "Use index -1 if none of them is the same food. "
                            "Do not report nutrition. Do not report a weight."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f'Detected: "{name}" (state: {state.value})\n\nCandidates:\n{listing}'
                        ),
                    },
                ],
                schema={
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["index", "confidence"],
                },
            )
            payload = json.loads(content or "{}")
            index = int(payload.get("index", -1))
            confidence = float(payload.get("confidence", 0.0))
        except Exception as exc:  # a tiebreak is an optimisation, never a blocker
            log.warning("resolution tiebreak failed, falling back to trigram: %s", exc)
            return None

        if not (0 <= index < len(candidates)):
            return None

        chosen = candidates[index]
        return Resolution(
            food_id=chosen.food_id,
            recipe_id=None,
            display_name=chosen.name,
            method=ResolutionMethod.llm_tiebreak,
            confidence=min(confidence, 0.95),
            candidates=tuple(candidates),
        )

    # --- d. new entry ------------------------------------------------------

    async def create_provisional(
        self,
        name: str,
        state: FoodState,
        macros_per_100g: tuple[float, float, float, float],
    ) -> Food:
        """A tier 4 row: model-generated, provisional, flagged for verification.

        Inevitable and useful, but never silently treated as fact. The
        calibration engine excludes windows these dominate.

        Note that the *production* tier-4 path is `core/enrichment.py`, which
        runs off the critical path and validates the composition arithmetically
        before writing. This constructor stays because the label-photo importer
        needs it and because it is the honest expression of what a tier-4 row
        is; it is not the path a missed photo item takes.
        """
        kcal, protein, carbs, fat = macros_per_100g
        food = Food(
            canonical_name_en=name,
            aliases=[],
            state=state,
            kcal_per_100g=kcal,
            protein_g_per_100g=protein,
            carbs_g_per_100g=carbs,
            fat_g_per_100g=fat,
            source=FoodSource.model,
            trust_tier=4,
        )
        self._db.add(food)
        await self._db.flush()
        return food


def _from(c: Candidate, method: ResolutionMethod, candidates: list[Candidate]) -> Resolution:
    return Resolution(
        food_id=c.food_id,
        recipe_id=None,
        display_name=c.name,
        method=method,
        confidence=c.score,
        candidates=tuple(candidates),
    )
