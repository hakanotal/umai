"""USDA FoodData Central.

Foundation and SR Legacy are laboratory generics and land as tier 1. Branded is
tier 2, and FNDDS (survey composites) is tier 2 as well: useful, but a modelled
average of how people report eating a dish rather than a direct analysis.

Free data.gov key, 1000 requests per hour per IP. DEMO_KEY works for a handful
of calls, which is enough to try this out before signing up.

Verified against the live API: nutrients arrive as a flat foodNutrients list
keyed by nutrientId, with values already per 100g.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from umai.db.models import FoodSource, FoodState

log = logging.getLogger(__name__)

BASE = "https://api.nal.usda.gov/fdc/v1"

# nutrientId, which is stable. nutrientNumber is the older INFOODS tag and the
# two do not agree, so pick one and never mix them.
ENERGY_KCAL = 1008
ENERGY_ATWATER = 2047  # some Foundation rows carry this instead of 1008
PROTEIN = 1003
FAT = 1004
CARBS = 1005
FIBER = 1079
SUGAR = 2000
SODIUM = 1093

TIER_BY_TYPE = {
    "Foundation": (FoodSource.usda_foundation, 1),
    "SR Legacy": (FoodSource.usda_sr, 1),
    "Survey (FNDDS)": (FoodSource.usda_sr, 2),
    "Branded": (FoodSource.usda_branded, 2),
}

# USDA descriptions state the preparation in prose. Mapping it is what keeps the
# raw-versus-cooked distinction, which is worth a factor of three on staples.
_STATE_WORDS: tuple[tuple[str, FoodState], ...] = (
    ("boiled", FoodState.boiled),
    ("cooked", FoodState.boiled),
    ("grilled", FoodState.grilled),
    ("broiled", FoodState.grilled),
    ("fried", FoodState.fried),
    ("baked", FoodState.baked),
    ("roasted", FoodState.roasted),
    ("steamed", FoodState.steamed),
    ("dried", FoodState.dried),
    ("dehydrated", FoodState.dried),
    ("raw", FoodState.raw),
    ("uncooked", FoodState.raw),
)


@dataclass(slots=True)
class ImportedFood:
    """A source-agnostic row, ready to become a Food."""

    canonical_name_en: str
    state: FoodState
    kcal_per_100g: float
    protein_g_per_100g: float
    carbs_g_per_100g: float
    fat_g_per_100g: float
    fiber_g_per_100g: float | None
    sugar_g_per_100g: float | None
    sodium_mg_per_100g: float | None
    source: FoodSource
    source_ref: str
    trust_tier: int
    aliases: list[str]
    density_g_per_ml: float | None = None


def infer_state(description: str) -> FoodState:
    text = description.lower()
    for word, state in _STATE_WORDS:
        if word in text:
            return state
    return FoodState.unknown


def tidy_name(description: str) -> str:
    """USDA writes "Lentils, mature seeds, cooked, boiled, with salt".

    Keep the head noun and drop the preparation clauses, which are captured in
    `state` instead. The full description is kept in source_ref, so nothing is
    lost, and the resolver matches against something a person would type.
    """
    head = description.split(",")[0].strip()
    return head.lower()


def _nutrient_map(food: dict[str, Any]) -> dict[int, float]:
    out: dict[int, float] = {}
    for n in food.get("foodNutrients") or []:
        # search results and detail responses nest this differently
        nid = n.get("nutrientId") or (n.get("nutrient") or {}).get("id")
        value = n.get("value") if "value" in n else n.get("amount")
        if nid is None or value is None:
            continue
        out[int(nid)] = float(value)
    return out


def to_imported(food: dict[str, Any]) -> ImportedFood | None:
    nutrients = _nutrient_map(food)
    kcal = nutrients.get(ENERGY_KCAL) or nutrients.get(ENERGY_ATWATER)
    if kcal is None:
        # No energy value means the row cannot price a portion. Skipped rather
        # than defaulted to zero, which would silently under-count a meal.
        log.debug("skipping %s: no energy value", food.get("description"))
        return None

    data_type = food.get("dataType") or ""
    source, tier = TIER_BY_TYPE.get(data_type, (FoodSource.usda_sr, 2))
    description = food.get("description") or ""

    aliases = [description.lower()]
    common = (food.get("commonNames") or "").strip()
    if common:
        aliases.extend(a.strip().lower() for a in common.split(";") if a.strip())

    return ImportedFood(
        canonical_name_en=tidy_name(description),
        state=infer_state(description),
        kcal_per_100g=kcal,
        protein_g_per_100g=nutrients.get(PROTEIN, 0.0),
        carbs_g_per_100g=nutrients.get(CARBS, 0.0),
        fat_g_per_100g=nutrients.get(FAT, 0.0),
        fiber_g_per_100g=nutrients.get(FIBER),
        sugar_g_per_100g=nutrients.get(SUGAR),
        sodium_mg_per_100g=nutrients.get(SODIUM),
        source=source,
        source_ref=f"fdc:{food.get('fdcId')} {description}"[:200],
        trust_tier=tier,
        aliases=sorted(set(aliases)),
    )


async def search(
    query: str,
    api_key: str = "DEMO_KEY",
    *,
    data_types: tuple[str, ...] = ("Foundation", "SR Legacy"),
    page_size: int = 5,
    client: httpx.AsyncClient | None = None,
) -> list[ImportedFood]:
    """Search FDC and return importable rows.

    Foundation and SR Legacy by default: those are the high-quality generics.
    Branded is available but belongs at tier 2 and is better reached by barcode.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        # dataType is an array parameter: repeated keys, not a comma-joined
        # string. Verified against the live API — the joined form 404s.
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("query", query),
            ("api_key", api_key),
            ("pageSize", page_size),
            *[("dataType", t) for t in data_types],
        ]
        resp = await client.get(f"{BASE}/foods/search", params=params)
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if owns_client:
            await client.aclose()

    rows = [to_imported(f) for f in payload.get("foods") or []]
    return [r for r in rows if r is not None]
