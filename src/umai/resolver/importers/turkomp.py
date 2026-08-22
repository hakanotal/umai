"""TurKomp, Turkey's national food composition database (tier 1).

Built by TUBITAK MAM to EuroFIR standards, and the only credible source for
Turkish dishes and local produce, which USDA covers poorly or not at all.

It is a web database rather than a documented public API, so there is no
scraper here on purpose: seeding means extracting the subset you actually eat,
which is a one-time job of a few hours covering perhaps 150-300 items. This
module imports that extract from a CSV you fill in, which keeps the provenance
honest (these are transcribed laboratory values, so tier 1) without pretending
an API exists.

CSV columns, header row required:

    name_en,aliases_tr,state,kcal,protein_g,carbs_g,fat_g,fiber_g,density,yield_factor,ref

`aliases_tr` is semicolon-separated. Everything after `fiber_g` is optional.
"""

from __future__ import annotations

import csv
from pathlib import Path

from umai.db.models import FoodSource, FoodState
from umai.resolver.importers.usda import ImportedFood


def _float(row: dict[str, str], key: str) -> float | None:
    raw = (row.get(key) or "").strip()
    if not raw:
        return None
    return float(raw)


def load_csv(path: Path) -> list[ImportedFood]:
    out: list[ImportedFood] = []
    with path.open(newline="", encoding="utf-8") as f:
        for lineno, row in enumerate(csv.DictReader(f), start=2):
            name = (row.get("name_en") or "").strip().lower()
            if not name:
                continue
            kcal = _float(row, "kcal")
            if kcal is None:
                raise ValueError(f"{path}:{lineno} '{name}' has no kcal value")

            aliases = [
                a.strip().lower() for a in (row.get("aliases_tr") or "").split(";") if a.strip()
            ]
            state_raw = (row.get("state") or "unknown").strip().lower()
            try:
                state = FoodState(state_raw)
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{lineno} '{state_raw}' is not a known state; "
                    f"one of {[s.value for s in FoodState]}"
                ) from exc

            out.append(
                ImportedFood(
                    canonical_name_en=name,
                    state=state,
                    kcal_per_100g=kcal,
                    protein_g_per_100g=_float(row, "protein_g") or 0.0,
                    carbs_g_per_100g=_float(row, "carbs_g") or 0.0,
                    fat_g_per_100g=_float(row, "fat_g") or 0.0,
                    fiber_g_per_100g=_float(row, "fiber_g"),
                    sugar_g_per_100g=_float(row, "sugar_g"),
                    sodium_mg_per_100g=_float(row, "sodium_mg"),
                    source=FoodSource.turkomp,
                    source_ref=(row.get("ref") or "turkomp").strip()[:200],
                    trust_tier=1,
                    aliases=aliases,
                    density_g_per_ml=_float(row, "density"),
                )
            )
    return out
