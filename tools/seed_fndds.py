"""Load data/fndds_seed.csv into the fndds_foods reference table.

The CSV is produced by tools/extract_fndds.py from the USDA survey-food
download. This script only moves it into Postgres, where the enrichment job can
search it with pg_trgm; it does no arithmetic and derives nothing, because
every number in the file is already measured data in the unit the table stores
(per 100 g of edible portion).

Idempotent by fdc_id, USDA's own stable identifier: re-running after a newer
FNDDS release updates the composition of rows that changed and inserts the ones
that are new, without duplicating anything or disturbing the `foods` rows that
were copied out of here earlier.

Two columns in the CSV are deliberately not loaded. `atwater_kcal` and
`atwater_ratio` are review aids for the human pass, recomputable at any time;
`state_hint` is a keyword guess that is blank for 92% of rows and wrong often
enough that a column with that name would be read as authoritative. The
preparation is in the description, where the model reads it.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Written by tools/extract_fndds.py. Anything else is a different file.
REQUIRED_COLUMNS = {
    "fdc_id",
    "food_code",
    "description",
    "wweia_category_description",
    "kcal_per_100g",
    "protein_g_per_100g",
    "carbs_g_per_100g",
    "fat_g_per_100g",
    "fiber_g_per_100g",
    "sugar_g_per_100g",
    "sodium_mg_per_100g",
}

# The columns a re-import is allowed to change. fdc_id is the key; everything
# else is USDA's to restate.
UPDATABLE = (
    "food_code",
    "description",
    "wweia_category",
    "kcal_per_100g",
    "protein_g_per_100g",
    "carbs_g_per_100g",
    "fat_g_per_100g",
    "fiber_g_per_100g",
    "sugar_g_per_100g",
    "sodium_mg_per_100g",
)

# Insert in chunks rather than one statement: 5,431 rows in a single VALUES list
# is a multi-megabyte query, and a chunked loop reports progress.
CHUNK = 500


def _opt_float(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return float(raw)


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(
                f"{path} is missing {sorted(missing)}; regenerate it with `just extract-fndds`"
            )
        out = []
        for r in reader:
            # kcal and the three macros are non-null in the table: the extractor
            # already drops any food that lacks them, so a blank here means the
            # CSV was edited by hand into a shape the schema will reject.
            try:
                values = {
                    "fdc_id": int(r["fdc_id"]),
                    "food_code": (r["food_code"] or None),
                    "description": r["description"],
                    "wweia_category": (r["wweia_category_description"] or None),
                    "kcal_per_100g": float(r["kcal_per_100g"]),
                    "protein_g_per_100g": float(r["protein_g_per_100g"]),
                    "carbs_g_per_100g": float(r["carbs_g_per_100g"]),
                    "fat_g_per_100g": float(r["fat_g_per_100g"]),
                    "fiber_g_per_100g": _opt_float(r["fiber_g_per_100g"]),
                    "sugar_g_per_100g": _opt_float(r["sugar_g_per_100g"]),
                    "sodium_mg_per_100g": _opt_float(r["sodium_mg_per_100g"]),
                }
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"row fdc_id={r.get('fdc_id')!r}: {exc}") from exc
            out.append(values)
        return out


async def seed(session, rows: list[dict]) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from umai.db.models import FnddsFood

    written = 0
    for start in range(0, len(rows), CHUNK):
        chunk = rows[start : start + CHUNK]
        stmt = pg_insert(FnddsFood).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FnddsFood.fdc_id],
            set_={c: getattr(stmt.excluded, c) for c in UPDATABLE},
        )
        await session.execute(stmt)
        written += len(chunk)
    return written


async def main_async(rows: list[dict], path: Path) -> int:
    from umai.config.settings import get_settings
    from umai.db.session import init_engine, session_scope

    settings = get_settings()
    init_engine(settings)
    try:
        async with session_scope() as session:
            written = await seed(session, rows)
        print(f"fndds: {written} row(s) inserted or updated from {path}")
    finally:
        from umai.db.session import dispose_engine

        await dispose_engine()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default="data/fndds_seed.csv")
    args = ap.parse_args()
    # Read the file here rather than inside the coroutine: blocking IO in async
    # code is the thing ASYNC240 is about, and there is no reason to open the
    # database before knowing the CSV parses.
    path = Path(args.csv)
    if not path.exists():
        raise SystemExit(f"no such file: {path}; generate it with `just extract-fndds`")
    return asyncio.run(main_async(_rows(path), path))


if __name__ == "__main__":
    raise SystemExit(main())
