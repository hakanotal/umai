"""Reduce the FNDDS survey-food CSV bundle to one inspectable seed table.

The USDA download is thirteen files and 19 MB of long-format nutrient rows; the
`foods` table wants one wide row per food with seven numbers on it. This script
does that reduction and nothing else. It writes CSVs for a human to read before
anything touches the database — the seeding step is deliberately separate.

Three properties of the survey-food bundle drive the implementation:

  * `food_nutrient.nutrient_id` does **not** hold `nutrient.id` for FNDDS foods.
    It holds `nutrient.nutrient_nbr`, the three-digit legacy code, as the
    "2021-2023 FNDDS crosswalk" sheet of the field-description workbook states
    ("Based on nutrient.nutrient_nbr = 'Nutrient code'"). Joining on `id` — the
    obvious reading of the column name — returns zero rows for every nutrient we
    want. We therefore resolve codes through `nutrient_nbr`, matched as an exact
    string: `205` is carbohydrate by difference but `205.2` is carbohydrate by
    summation, and any numeric coercion collapses the two.
  * Amounts are per 100 g of edible portion, which is already the unit the
    `foods` table stores. No conversion, and none should be introduced.
  * Carbohydrate is *by difference*, so it includes fibre. That is the same
    convention the rest of the pipeline assumes, so fibre is carried alongside
    rather than subtracted.

The Atwater columns are a review aid, not a gate. FNDDS energy is computed with
food-specific Atwater factors, so 4P+4C+9F reconstructs it to within a few per
cent rather than exactly; `core/enrichment.py` can afford a hard check because
it is judging one invented row, and this is 5431 measured ones. A large residual
here means "read this row before trusting it".

`state_hint` is a keyword guess at the `food_state` enum, offered so the manual
pass has somewhere to start. It is wrong often enough that it must never be
written to the database unread.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

# nutrient_nbr codes, as exact strings. See the module docstring on 205/205.2.
NUTRIENTS: dict[str, tuple[str, str]] = {
    "208": ("kcal_per_100g", "KCAL"),
    "203": ("protein_g_per_100g", "G"),
    "205": ("carbs_g_per_100g", "G"),
    "204": ("fat_g_per_100g", "G"),
    "291": ("fiber_g_per_100g", "G"),
    "269": ("sugar_g_per_100g", "G"),
    "307": ("sodium_mg_per_100g", "MG"),
}

# Checked against nutrient.csv at load time; a silent unit change upstream would
# otherwise turn milligrams of sodium into grams without anything noticing.
MACRO_FIELDS = ("protein_g_per_100g", "carbs_g_per_100g", "fat_g_per_100g")

# Ordered: the first pattern to appear in the description wins, so
# "chicken, fried, boiled" is fried. Patterns are matched against the
# comma-separated fragments, not the raw string, so "fried rice" as a dish name
# and ", fried" as a preparation are distinguished by the caller reading the row.
STATE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fried", ("fried", "deep fried", "pan fried", "batter fried")),
    ("baked", ("baked", "oven baked")),
    ("roasted", ("roasted", "roast")),
    ("grilled", ("grilled", "broiled", "barbecued")),
    ("boiled", ("boiled", "cooked in water", "poached", "simmered")),
    ("steamed", ("steamed",)),
    ("dried", ("dried", "dehydrated", "freeze dried")),
    ("raw", ("raw", "uncooked", "fresh")),
)


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _num(raw: str) -> float | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _state_hint(description: str) -> str:
    fragments = [f.strip().lower() for f in description.split(",")]
    for state, patterns in STATE_HINTS:
        for fragment in fragments:
            if fragment in patterns:
                return state
    return ""


def _check_units(nutrient_rows: list[dict[str, str]]) -> None:
    """Fail loudly if USDA ever restates a nutrient in a different unit."""
    by_nbr = {r["nutrient_nbr"]: r for r in nutrient_rows}
    for code, (field, expected) in NUTRIENTS.items():
        row = by_nbr.get(code)
        if row is None:
            sys.exit(f"nutrient_nbr {code} ({field}) absent from nutrient.csv")
        if row["unit_name"].strip().upper() != expected:
            sys.exit(
                f"unit drift: nutrient_nbr {code} ({row['name']}) is "
                f"{row['unit_name']}, expected {expected}"
            )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--src",
        type=Path,
        default=Path("data/FoodData_Central_survey_food_csv_2024-10-31"),
        help="the unzipped FNDDS survey-food CSV directory",
    )
    ap.add_argument("--out", type=Path, default=Path("data/fndds_seed.csv"))
    ap.add_argument(
        "--portions-out",
        type=Path,
        default=Path("data/fndds_portions.csv"),
        help="household portion gram weights, written as a companion file",
    )
    args = ap.parse_args()

    src: Path = args.src
    if not src.is_dir():
        sys.exit(f"no such directory: {src}")

    nutrient_rows = _read(src / "nutrient.csv")
    _check_units(nutrient_rows)

    foods = _read(src / "food.csv")
    survey = {r["fdc_id"]: r for r in _read(src / "survey_fndds_food.csv")}
    categories = {
        r["wweia_food_category"]: r["wweia_food_category_description"]
        for r in _read(src / "wweia_food_category.csv")
    }

    # One pass over the 353k-row long table, keeping only the seven codes.
    values: dict[str, dict[str, float]] = defaultdict(dict)
    for row in _read(src / "food_nutrient.csv"):
        target = NUTRIENTS.get(row["nutrient_id"])
        if target is None:
            continue
        amount = _num(row["amount"])
        if amount is None:
            continue
        values[row["fdc_id"]][target[0]] = amount

    fields = [
        "fdc_id",
        "food_code",
        "description",
        "wweia_category_number",
        "wweia_category_description",
        "state_hint",
        "kcal_per_100g",
        "protein_g_per_100g",
        "carbs_g_per_100g",
        "fat_g_per_100g",
        "fiber_g_per_100g",
        "sugar_g_per_100g",
        "sodium_mg_per_100g",
        "atwater_kcal",
        "atwater_ratio",
        "publication_date",
    ]

    written = 0
    skipped_no_nutrients: list[str] = []
    incomplete: list[str] = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for food in sorted(foods, key=lambda f: f["description"].lower()):
            fdc_id = food["fdc_id"]
            vals = values.get(fdc_id)
            if not vals:
                skipped_no_nutrients.append(f"{fdc_id} {food['description']}")
                continue
            if "kcal_per_100g" not in vals or any(f not in vals for f in MACRO_FIELDS):
                incomplete.append(f"{fdc_id} {food['description']}")
                continue

            s = survey.get(fdc_id, {})
            cat = s.get("wweia_category_number", "")
            kcal = vals["kcal_per_100g"]
            atwater = (
                4 * vals["protein_g_per_100g"]
                + 4 * vals["carbs_g_per_100g"]
                + 9 * vals["fat_g_per_100g"]
            )
            record = {
                "fdc_id": fdc_id,
                "food_code": s.get("food_code", ""),
                "description": food["description"],
                "wweia_category_number": cat,
                "wweia_category_description": categories.get(cat, ""),
                "state_hint": _state_hint(food["description"]),
                "atwater_kcal": f"{atwater:.1f}",
                # Blank rather than a division by zero for water, coffee, tea.
                "atwater_ratio": f"{atwater / kcal:.3f}" if kcal > 0 else "",
                "publication_date": food["publication_date"],
            }
            for field, _unit in NUTRIENTS.values():
                v = vals.get(field)
                record[field] = "" if v is None else f"{v:g}"
            writer.writerow(record)
            written += 1

    # Portions: survey foods embed the amount in the description ("1 cup"), and
    # carry a zero-weight "Quantity not specified" row that is a placeholder,
    # not a measure. Both facts come from the field-description workbook.
    portion_rows = 0
    descriptions = {f["fdc_id"]: f["description"] for f in foods}
    with args.portions_out.open("w", newline="", encoding="utf-8") as fh:
        pw = csv.DictWriter(
            fh,
            fieldnames=["fdc_id", "food_code", "description", "portion_description", "gram_weight"],
        )
        pw.writeheader()
        for row in _read(src / "food_portion.csv"):
            grams = _num(row["gram_weight"])
            if grams is None or grams <= 0:
                continue
            fdc_id = row["fdc_id"]
            pw.writerow(
                {
                    "fdc_id": fdc_id,
                    "food_code": survey.get(fdc_id, {}).get("food_code", ""),
                    "description": descriptions.get(fdc_id, ""),
                    "portion_description": row["portion_description"],
                    "gram_weight": f"{grams:g}",
                }
            )
            portion_rows += 1

    print(f"{args.out}: {written} rows")
    print(f"{args.portions_out}: {portion_rows} rows")
    for label, items in (("no nutrient rows", skipped_no_nutrients), ("incomplete", incomplete)):
        if items:
            print(f"skipped, {label}: {len(items)}")
            for item in items[:10]:
                print(f"  {item}")


if __name__ == "__main__":
    main()
