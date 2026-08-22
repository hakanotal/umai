"""Fuzzy lookup over the USDA FNDDS reference table.

The enrichment job's model reaches this through a tool call. It exists because
everything that module writes today is *recalled* — the model's memory of what
is in a dish — and 5,431 rows of measured USDA composition are a better answer
whenever one of them is actually the food in question.

**This table is English and American.** That is not a caveat to work around, it
is the boundary of what the tool is for: English food names and specific
ingredients. It holds "Soup, lentil", "Cheese, Feta" and "Chicken fillet,
grilled"; it holds no lahmacun, no ayran, no simit. Searching it for a Turkish
dish name is not a near miss, it is a category error, and the model is told so.

The threshold enforces that for free, which is the pleasing part. Measured
against all 5,431 descriptions:

    lahmacun     0.182   ->  Lau lau
    doner kebab  0.125   ->  Kefir
    ayran        0.133   ->  Wheat bran
    ---------------------------- 0.30
    scrambled eggs        0.351  ->  Egg omelet or scrambled egg, no added fat
    french fries          0.500  ->  Potato, french fries, NFS
    lentil soup           1.000  ->  Soup, lentil

Fourteen Turkish transliterations peak at 0.231; twelve English generic names
bottom out at 0.351. Nothing lives in between, so MIN_SCORE = 0.30 returns the
right row for an English name and *nothing at all* for a name this table does
not cover — rather than confidently returning Lard for lahmacun. The same 0.30
is resolver/match.py's FLOOR, arrived at independently.

What the threshold cannot do is catch a confident wrong match: "turkish tea"
scores 0.444 against "Coffee, Turkish". Ranking is not judgement. The model
picks, and the fdc_id it picks is recorded on the foods row so the choice can be
read back later.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from umai.db.models import FnddsFood

# Below this a match is noise. See the module docstring for the measurement;
# a test in tests/unit pins it against the shipped CSV so a future FNDDS
# release that closes the gap fails loudly rather than silently degrading.
MIN_SCORE = 0.30

# Five is what the resolver offers its tiebreak model, for the same reason:
# enough to contain the right answer, few enough that choosing is still a
# judgement rather than a search.
LIMIT = 5


@dataclass(slots=True)
class FnddsHit:
    """One candidate row, with the numbers the model needs to choose between."""

    fdc_id: int
    description: str
    category: str | None
    score: float
    kcal_per_100g: float
    protein_g_per_100g: float
    carbs_g_per_100g: float
    fat_g_per_100g: float
    fiber_g_per_100g: float | None

    def as_tool_result(self) -> dict[str, object]:
        """The shape handed back to the model. Deliberately not the ORM row.

        The model gets the fdc_id, the description and the composition, and
        nothing else: no trust tier to reason about, no internal ids, and no
        prose it might echo into a name. Rounded because the extra digits are
        tokens spent on precision the source does not have.
        """
        return {
            "fdc_id": self.fdc_id,
            "description": self.description,
            "category": self.category,
            "match_score": round(self.score, 3),
            "kcal_per_100g": round(self.kcal_per_100g, 1),
            "protein_g_per_100g": round(self.protein_g_per_100g, 2),
            "carbs_g_per_100g": round(self.carbs_g_per_100g, 2),
            "fat_g_per_100g": round(self.fat_g_per_100g, 2),
            "fiber_g_per_100g": (
                None if self.fiber_g_per_100g is None else round(self.fiber_g_per_100g, 2)
            ),
        }


async def search(
    session: AsyncSession,
    query: str,
    *,
    limit: int = LIMIT,
    min_score: float = MIN_SCORE,
) -> list[FnddsHit]:
    """Trigram-match `query` against FNDDS descriptions, best first.

    Returns an empty list rather than a poor match when nothing clears
    `min_score`; an empty result is the tool's way of saying "this table does
    not cover that", which is a useful answer and the common one for anything
    not named in English.

    No `func.lower()` on the column: pg_trgm normalises case itself, so the
    wrapper resolver/match.py uses is redundant here and would defeat the
    gin_trgm_ops index on `description`.
    """
    query = " ".join(query.strip().split())
    if not query:
        return []

    score = func.similarity(FnddsFood.description, query).label("score")
    rows = (
        await session.execute(
            select(FnddsFood, score).where(score >= min_score).order_by(score.desc()).limit(limit)
        )
    ).all()

    return [
        FnddsHit(
            fdc_id=row.FnddsFood.fdc_id,
            description=row.FnddsFood.description,
            category=row.FnddsFood.wweia_category,
            score=float(row.score),
            kcal_per_100g=row.FnddsFood.kcal_per_100g,
            protein_g_per_100g=row.FnddsFood.protein_g_per_100g,
            carbs_g_per_100g=row.FnddsFood.carbs_g_per_100g,
            fat_g_per_100g=row.FnddsFood.fat_g_per_100g,
            fiber_g_per_100g=row.FnddsFood.fiber_g_per_100g,
        )
        for row in rows
    ]


async def by_ids(session: AsyncSession, fdc_ids: list[int]) -> dict[int, FnddsFood]:
    """The rows behind a set of ids the model chose.

    The composition written to `foods` is read from here, never from the model's
    message: the model's contribution is which row, and code does the copying.
    That is the same division resolver/match.py keeps for its tiebreak, and it
    is what makes a tier-2 row from this path honest.
    """
    if not fdc_ids:
        return {}
    rows = (
        (await session.execute(select(FnddsFood).where(FnddsFood.fdc_id.in_(fdc_ids))))
        .scalars()
        .all()
    )
    return {row.fdc_id: row for row in rows}
