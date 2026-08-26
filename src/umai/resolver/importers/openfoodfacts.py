"""Open Food Facts, packaged and barcoded products (tier 2). No API key.

Roughly 4.7 million products under an open licence. Coverage of Turkish
supermarket products is uneven, which is the honest reason barcode scanning is
a later phase and the label-photo importer is the universal fallback.

Nutriments arrive per 100g already, under `*_100g` keys. Energy is published in
kJ (`energy-kcal_100g` is usually present too, but not always).
"""

from __future__ import annotations

from typing import Any

from umai.db.models import FoodSource, FoodState
from umai.resolver.importers.usda import ImportedFood

KJ_PER_KCAL = 4.184


def _kcal(nutriments: dict[str, Any]) -> float | None:
    kcal = nutriments.get("energy-kcal_100g")
    if kcal is not None:
        return float(kcal)
    kj = nutriments.get("energy_100g") or nutriments.get("energy-kj_100g")
    if kj is not None:
        return float(kj) / KJ_PER_KCAL
    return None


def to_imported(product: dict[str, Any], barcode: str) -> ImportedFood | None:
    nutriments = product.get("nutriments") or {}
    kcal = _kcal(nutriments)
    if kcal is None:
        return None

    name = (product.get("product_name_en") or product.get("product_name") or "").strip().lower()
    if not name:
        return None

    brand = (product.get("brands") or "").split(",")[0].strip()
    aliases = [a for a in {name, f"{brand} {name}".strip().lower()} if a]

    return ImportedFood(
        canonical_name_en=f"{brand} {name}".strip().lower() if brand else name,
        # A packaged product is sold in the state it is eaten in; the label
        # already describes that, so there is nothing to infer.
        state=FoodState.unknown,
        kcal_per_100g=kcal,
        protein_g_per_100g=float(nutriments.get("proteins_100g") or 0.0),
        carbs_g_per_100g=float(nutriments.get("carbohydrates_100g") or 0.0),
        fat_g_per_100g=float(nutriments.get("fat_100g") or 0.0),
        fiber_g_per_100g=_opt(nutriments.get("fiber_100g")),
        sugar_g_per_100g=_opt(nutriments.get("sugars_100g")),
        sodium_mg_per_100g=_mg(nutriments.get("sodium_100g")),
        source=FoodSource.off,
        source_ref=f"off:{barcode}",
        trust_tier=2,
        aliases=sorted(set(aliases)),
    )


def _opt(v: Any) -> float | None:
    return float(v) if v is not None else None


def _mg(grams: Any) -> float | None:
    """OFF publishes sodium in grams; the foods table stores milligrams."""
    return float(grams) * 1000.0 if grams is not None else None
