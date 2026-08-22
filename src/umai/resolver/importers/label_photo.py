"""Nutrition label photo to a tier 2 foods row.

The universal fallback, and a far better use of vision than portion estimation:
transcription rather than judgement. It fills coverage gaps for exactly the
local products the open databases miss.

Two things this must get right, because EU labels make both easy to get wrong:

  * Energy is printed in kJ and kcal together. Take the kcal.
  * Values may be printed per serving rather than per 100g, or both. The foods
    table stores per 100g, so a per-serving-only label must be converted using
    the serving size, and if that is missing the row is refused rather than
    guessed at.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from umai.config.models import ModelClient
from umai.db.models import FoodSource, FoodState
from umai.perception.client import parse_json_object
from umai.perception.images import prepare_for_model
from umai.resolver.importers.usda import ImportedFood

KJ_PER_KCAL = 4.184

SYSTEM = """You transcribe nutrition labels. You are reading, not estimating.

Report the values exactly as printed. Do not convert, round or infer anything
beyond what the label states. If a value is not printed, leave it null; a null
is correct and a guess is not.

If the label gives values per serving as well as per 100g, report the per-100g
column. If it gives ONLY per serving, report those numbers and set
basis="serving" along with the serving size in grams.

Energy: report kcal. European labels print kJ first; do not confuse them."""


class LabelReading(BaseModel):
    model_config = ConfigDict(extra="ignore")

    product_name: str
    brand: str | None = None
    basis: str = Field(default="100g", description='"100g" or "serving"')
    serving_size_g: float | None = None
    kcal: float | None = None
    kj: float | None = None
    protein_g: float | None = None
    carbs_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    sugar_g: float | None = None
    sodium_mg: float | None = None
    salt_g: float | None = None


class LabelUnreadable(ValueError):
    pass


def read_label(photo: Path, models: ModelClient) -> LabelReading:
    image_b64, media_type = prepare_for_model(photo)
    content, _served, _ms = models.call(
        "label_ocr",
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "Transcribe this nutrition label."},
        ],
        schema=LabelReading.model_json_schema(),
        image_b64=image_b64,
        media_type=media_type,
    )
    return LabelReading.model_validate(parse_json_object(content))


def to_imported(reading: LabelReading) -> ImportedFood:
    """Normalise a label reading to a per-100g row.

    Refuses rather than guesses when the arithmetic is not available: a wrong
    per-100g figure propagates into every future portion of that product.
    """
    kcal = reading.kcal
    if kcal is None and reading.kj is not None:
        kcal = reading.kj / KJ_PER_KCAL
    if kcal is None:
        raise LabelUnreadable("no energy value on the label")

    values: dict[str, float | None] = {
        "kcal": kcal,
        "protein": reading.protein_g,
        "carbs": reading.carbs_g,
        "fat": reading.fat_g,
        "fiber": reading.fiber_g,
        "sugar": reading.sugar_g,
        "sodium_mg": _sodium_mg(reading),
    }

    if reading.basis == "serving":
        if not reading.serving_size_g or reading.serving_size_g <= 0:
            raise LabelUnreadable(
                "label gives per-serving values but no serving size, so they "
                "cannot be converted to per 100g"
            )
        factor = 100.0 / reading.serving_size_g
        values = {k: (v * factor if v is not None else None) for k, v in values.items()}

    name = " ".join(
        p for p in [(reading.brand or "").strip(), reading.product_name.strip()] if p
    ).lower()

    return ImportedFood(
        canonical_name_en=name,
        state=FoodState.unknown,
        kcal_per_100g=_req(values["kcal"]),
        protein_g_per_100g=values["protein"] or 0.0,
        carbs_g_per_100g=values["carbs"] or 0.0,
        fat_g_per_100g=values["fat"] or 0.0,
        fiber_g_per_100g=values["fiber"],
        sugar_g_per_100g=values["sugar"],
        sodium_mg_per_100g=values["sodium_mg"],
        source=FoodSource.label_photo,
        source_ref="label photo",
        # Legally required accuracy, though tolerances are wide.
        trust_tier=2,
        aliases=[reading.product_name.strip().lower()],
    )


def _sodium_mg(reading: LabelReading) -> float | None:
    if reading.sodium_mg is not None:
        return reading.sodium_mg
    # EU labels print salt, not sodium. Salt is 39.34% sodium by mass.
    if reading.salt_g is not None:
        return reading.salt_g * 1000.0 * 0.3934
    return None


def _req(v: float | None) -> float:
    if v is None:
        raise LabelUnreadable("energy value missing after conversion")
    return v
