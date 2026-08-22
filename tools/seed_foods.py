#!/usr/bin/env python3
"""Seed the foods table from USDA, TurKomp and Open Food Facts.

Usage: uv run python tools/seed_foods.py --source usda --top 200

Phase 0.5's goal in one sentence: type any of your twenty most common foods
and get a tier 1 or 2 row back. USDA search covers the Western generics; the
Turkish staples need either a USDA hit (rice, lentils, bulgur exist) or the
TurKomp CSV once it is filled in (data/turkomp.csv, see
resolver/importers/turkomp.py for the column format).

DEMO_KEY is throttled hard; set USDA_API_KEY for a real run (free, instant,
1,000 requests/hour). Queries sleep between calls either way.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# The Phase 0.5 seed list: Western generics via USDA search, with the Turkish
# aliases the resolver should match on. Curated, not exhaustive — the label
# photo importer fills gaps forever after.
SEED: list[tuple[str, list[str]]] = [
    ("chicken breast", ["tavuk gogus"]),
    ("chicken thigh", ["tavuk but"]),
    ("ground beef", ["kiyma"]),
    ("beef steak", ["bonfile", "antrikot"]),
    ("egg", ["yumurta", "yumurta haqlanmis"]),
    ("white rice", ["pirinc", "pilav"]),
    ("brown rice", ["esmer pirinc"]),
    ("bulgur", ["bulgur pilavi"]),
    ("lentils", ["mercimek", "mercimek corbasi"]),
    ("chickpeas", ["nohut"]),
    ("kidney beans", ["kuru fasulye"]),
    ("white bread", ["ekmek", "tost ekmegi"]),
    ("whole wheat bread", ["tam bugday ekmegi"]),
    ("pasta", ["makarna", "spagetti"]),
    ("potato", ["patates"]),
    ("sweet potato", ["tatli patates"]),
    ("tomato", ["domates"]),
    ("cucumber", ["salatalik"]),
    ("onion", ["sogan"]),
    ("pepper", ["biber", "kirmizi biber"]),
    ("lettuce", ["marul"]),
    ("yogurt", ["yogurt", "suzme yogurt"]),
    ("feta cheese", ["beyaz peynir"]),
    ("kashkaval cheese", ["kasar peyniri"]),
    ("mozzarella", []),
    ("milk", ["sut"]),
    ("ayran", []),
    ("olive oil", ["zeytinyagi"]),
    ("butter", ["tereyagi"]),
    ("honey", ["bal"]),
    ("sugar", ["seker"]),
    ("banana", ["muz"]),
    ("apple", ["elma"]),
    ("orange", ["portakal"]),
    ("strawberry", ["cilek"]),
    ("grapes", ["uzum"]),
    ("oats", ["yulaf"]),
    ("walnuts", ["ceviz"]),
    ("hazelnuts", ["findik"]),
    ("almonds", ["badem"]),
    ("pistachios", ["antep fistigi"]),
    ("coffee", ["kahve", "turk kahvesi"]),
    ("tea", ["cay"]),
    ("salmon", ["somon"]),
    ("tuna", ["ton baligi"]),
    ("shrimp", ["karides"]),
    ("spinach", ["ispanak"]),
    ("broccoli", ["brokoli"]),
    ("cauliflower", ["karnabahar"]),
    ("zucchini", ["kabak"]),
    ("eggplant", ["patlican"]),
    ("okra", ["bamya"]),
    ("green beans", ["taze fasulye"]),
    ("mushrooms", ["mantar"]),
    ("garlic", ["sarmisak"]),
    ("chickpea flour", ["nohut unu"]),
    ("wheat flour", ["un"]),
    ("dark chocolate", ["bitter cikolata"]),
    ("milk chocolate", ["sutlu cikolata"]),
    ("ice cream", ["dondurma"]),
    ("orange juice", ["portakal suyu"]),
    ("cola", ["kola"]),
]


async def seed_usda(
    session, api_key: str, queries: list[tuple[str, list[str]]], sleep_s: float
) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from umai.db.models import Food
    from umai.resolver.importers import usda

    written = 0
    client = None
    try:
        import httpx

        client = httpx.AsyncClient(timeout=30)
        for query, aliases in queries:
            rows: list = []
            for attempt in range(3):
                try:
                    rows = await usda.search(query, api_key, page_size=3, client=client)
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429 and attempt < 2:
                        # DEMO_KEY throttles hard; back off and try again.
                        await asyncio.sleep(15 * (attempt + 1))
                        continue
                    break
                except httpx.HTTPError:
                    break
            if not rows:
                print(f"  -- {query}: nothing found (throttled or absent)")
                continue
            await asyncio.sleep(sleep_s)

            # Prefer the row whose state is known; the rest still land.
            for imp in rows:
                existing_aliases = set(imp.aliases)
                if aliases:
                    existing_aliases.update(aliases)
                values = dict(
                    canonical_name_en=imp.canonical_name_en,
                    aliases=sorted(existing_aliases),
                    state=imp.state,
                    kcal_per_100g=imp.kcal_per_100g,
                    protein_g_per_100g=imp.protein_g_per_100g,
                    carbs_g_per_100g=imp.carbs_g_per_100g,
                    fat_g_per_100g=imp.fat_g_per_100g,
                    fiber_g_per_100g=imp.fiber_g_per_100g,
                    sugar_g_per_100g=imp.sugar_g_per_100g,
                    sodium_mg_per_100g=imp.sodium_mg_per_100g,
                    density_g_per_ml=imp.density_g_per_ml,
                    source=imp.source,
                    source_ref=imp.source_ref,
                    trust_tier=imp.trust_tier,
                )
                stmt = (
                    pg_insert(Food)
                    .values(**values)
                    .on_conflict_do_update(
                        constraint="uq_foods_name_state",
                        set_={
                            k: values[k]
                            for k in (
                                "kcal_per_100g",
                                "protein_g_per_100g",
                                "carbs_g_per_100g",
                                "fat_g_per_100g",
                                "fiber_g_per_100g",
                                "sodium_mg_per_100g",
                                "aliases",
                            )
                        },
                    )
                )
                await session.execute(stmt)
                written += 1
            print(f"  ok {query}: {len(rows)} row(s), best state={rows[0].state.value}")
            await session.commit()  # per query: a throttle or crash keeps the rest
    finally:
        if client is not None:
            await client.aclose()
    return written


# ---------------------------------------------------------------------------
# The starter set: USDA Foundation values, transcribed by hand.
#
# Exists so the resolver has honest tier-1 rows to match before the full API
# import runs (DEMO_KEY is throttled to near-uselessness; a real key is free
# and instant at https://fdc.nal.usda.gov/api-key-signup.html). These are the
# published Foundation numbers, not estimates; `--source usda` upserts over
# them with live rows where queries succeed.
#
# (name, aliases, state, kcal, protein, carbs, fat, fiber, density, yield)
# ---------------------------------------------------------------------------

STARTER: list[
    tuple[str, list[str], str, float, float, float, float, float | None, float | None, float | None]
] = [
    ("chicken breast", ["tavuk gogus"], "grilled", 165.0, 31.0, 0.0, 3.6, 0.0, None, None),
    ("chicken breast", ["tavuk gogus"], "raw", 120.0, 22.5, 0.0, 2.6, 0.0, None, 0.75),
    ("egg", ["yumurta"], "boiled", 155.0, 12.6, 1.1, 10.6, 0.0, None, None),
    ("egg", ["yumurta"], "raw", 143.0, 12.6, 0.7, 9.5, 0.0, None, None),
    ("white rice", ["pirinc", "pilav"], "boiled", 130.0, 2.7, 28.0, 0.3, 0.4, None, None),
    ("white rice", ["pirinc"], "raw", 365.0, 7.1, 80.0, 0.7, 1.3, None, 2.7),
    ("brown rice", ["esmer pirinc"], "boiled", 123.0, 2.7, 25.6, 1.0, 1.6, None, None),
    ("bulgur", ["bulgur pilavi"], "boiled", 83.0, 3.1, 18.6, 0.2, 4.5, None, None),
    ("pasta", ["makarna", "spagetti"], "boiled", 158.0, 5.8, 30.9, 0.9, 1.8, None, None),
    ("potato", ["patates"], "boiled", 87.0, 1.9, 20.1, 0.1, 1.8, None, None),
    ("potato", ["patates"], "raw", 77.0, 2.0, 17.5, 0.1, 2.2, None, None),
    ("sweet potato", ["tatli patates"], "baked", 90.0, 2.0, 20.7, 0.2, 3.3, None, None),
    ("tomato", ["domates"], "raw", 18.0, 0.9, 3.9, 0.2, 1.2, None, None),
    ("cucumber", ["salatalik"], "raw", 15.0, 0.7, 3.6, 0.1, 0.5, None, None),
    ("onion", ["sogan"], "raw", 40.0, 1.1, 9.3, 0.1, 1.7, None, None),
    ("lettuce", ["marul"], "raw", 15.0, 1.4, 2.9, 0.2, 1.3, None, None),
    ("yogurt", ["yogurt"], "liquid", 61.0, 3.5, 4.7, 3.3, 0.0, 1.03, None),
    ("milk", ["sut"], "liquid", 61.0, 3.2, 4.8, 3.3, 0.0, 1.03, None),
    ("feta cheese", ["beyaz peynir"], "raw", 264.0, 14.2, 4.1, 21.3, 0.0, None, None),
    ("kashkaval cheese", ["kasar peyniri"], "raw", 356.0, 24.9, 1.9, 27.4, 0.0, None, None),
    ("olive oil", ["zeytinyagi"], "liquid", 884.0, 0.0, 0.0, 100.0, 0.0, 0.92, None),
    ("butter", ["tereyagi"], "raw", 717.0, 0.9, 0.1, 81.1, 0.0, 0.96, None),
    ("white bread", ["ekmek"], "baked", 265.0, 9.0, 49.0, 3.2, 2.7, None, None),
    ("whole wheat bread", ["tam bugday ekmegi"], "baked", 247.0, 13.0, 41.0, 3.5, 7.0, None, None),
    ("banana", ["muz"], "raw", 89.0, 1.1, 22.8, 0.3, 2.6, None, None),
    ("apple", ["elma"], "raw", 52.0, 0.3, 13.8, 0.2, 2.4, None, None),
    ("orange", ["portakal"], "raw", 47.0, 0.9, 11.8, 0.1, 2.4, None, None),
    ("strawberry", ["cilek"], "raw", 32.0, 0.7, 7.7, 0.3, 2.0, None, None),
    ("oats", ["yulaf"], "raw", 389.0, 16.9, 66.3, 6.9, 10.6, None, None),
    ("walnuts", ["ceviz"], "raw", 654.0, 15.2, 13.7, 65.2, 6.7, None, None),
    ("almonds", ["badem"], "raw", 579.0, 21.2, 21.6, 49.9, 12.5, None, None),
    ("spinach", ["ispanak"], "raw", 23.0, 2.9, 3.6, 0.4, 2.2, None, None),
    ("broccoli", ["brokoli"], "raw", 34.0, 2.8, 6.6, 0.4, 2.6, None, None),
    ("eggplant", ["patlican"], "raw", 25.0, 1.0, 5.9, 0.2, 3.0, None, None),
    ("zucchini", ["kabak"], "raw", 17.0, 1.2, 3.1, 0.3, 1.0, None, None),
    ("mushrooms", ["mantar"], "raw", 22.0, 3.1, 3.3, 0.3, 1.0, None, None),
    ("salmon", ["somon"], "grilled", 206.0, 22.1, 0.0, 12.4, 0.0, None, None),
    ("beef steak", ["bonfile", "antrikot"], "grilled", 271.0, 25.4, 0.0, 18.5, 0.0, None, None),
    ("ground beef", ["kiyma"], "grilled", 254.0, 26.1, 0.0, 15.9, 0.0, None, None),
    ("lentils", ["mercimek"], "boiled", 116.0, 9.0, 20.1, 0.4, 7.9, None, None),
    ("chickpeas", ["nohut"], "boiled", 164.0, 8.9, 27.4, 2.6, 7.6, None, None),
    ("kidney beans", ["kuru fasulye"], "boiled", 127.0, 8.7, 22.8, 0.5, 6.4, None, None),
    ("coffee", ["kahve", "turk kahvesi"], "liquid", 1.0, 0.1, 0.0, 0.0, 0.0, 1.0, None),
    ("tea", ["cay"], "liquid", 1.0, 0.0, 0.3, 0.0, 0.0, 1.0, None),
    ("honey", ["bal"], "liquid", 304.0, 0.3, 82.4, 0.0, 0.2, 1.42, None),
    ("dark chocolate", ["bitter cikolata"], "raw", 546.0, 7.8, 45.9, 31.0, 10.9, None, None),
]


async def seed_starter(session) -> int:
    """The hand-transcribed Foundation set above. Upserted so the live USDA
    import later replaces any row it finds a search hit for."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from umai.db.models import Food, FoodSource, FoodState

    written = 0
    for name, aliases, state, kcal, protein, carbs, fat, fiber, density, yf in STARTER:
        values = dict(
            canonical_name_en=name,
            aliases=sorted(set(aliases)),
            state=FoodState(state),
            kcal_per_100g=kcal,
            protein_g_per_100g=protein,
            carbs_g_per_100g=carbs,
            fat_g_per_100g=fat,
            fiber_g_per_100g=fiber,
            density_g_per_ml=density,
            yield_factor=yf,
            source=FoodSource.usda_foundation,
            source_ref="usda:foundation-starter (hand-transcribed)",
            trust_tier=1,
        )
        stmt = (
            pg_insert(Food)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_foods_name_state")
        )
        await session.execute(stmt)
        written += 1
    await session.commit()
    print(f"  starter: {written} row(s) (existing rows untouched)")
    return written


async def seed_turkomp(session, csv_path: Path) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from umai.db.models import Food
    from umai.resolver.importers import turkomp

    if not csv_path.exists():  # noqa: ASYNC240 - one stat call at startup
        print(f"  -- {csv_path} does not exist; skipping TurKomp")
        return 0
    imported = turkomp.load_csv(csv_path)
    for imp in imported:
        values = dict(
            canonical_name_en=imp.canonical_name_en,
            aliases=imp.aliases,
            state=imp.state,
            kcal_per_100g=imp.kcal_per_100g,
            protein_g_per_100g=imp.protein_g_per_100g,
            carbs_g_per_100g=imp.carbs_g_per_100g,
            fat_g_per_100g=imp.fat_g_per_100g,
            fiber_g_per_100g=imp.fiber_g_per_100g,
            sodium_mg_per_100g=imp.sodium_mg_per_100g,
            density_g_per_ml=imp.density_g_per_ml,
            source=imp.source,
            source_ref=imp.source_ref,
            trust_tier=imp.trust_tier,
        )
        stmt = (
            pg_insert(Food)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_foods_name_state",
                set_={"kcal_per_100g": values["kcal_per_100g"]},
            )
        )
        await session.execute(stmt)
    await session.commit()
    print(f"  turkomp: {len(imported)} row(s)")
    return len(imported)


async def main_async(args) -> int:
    from umai.config.settings import get_settings
    from umai.db.session import init_engine, session_scope

    settings = get_settings()
    init_engine(settings)
    try:
        async with session_scope() as session:
            if args.source in ("starter", "all"):
                await seed_starter(session)
            if args.source in ("usda", "all"):
                key = os.environ.get("USDA_API_KEY", "DEMO_KEY")
                n = await seed_usda(session, key, SEED, sleep_s=args.sleep)
                print(f"usda: wrote/updated {n} row(s)")
            if args.source in ("turkomp", "all"):
                await seed_turkomp(session, Path(args.turkomp_csv))
    finally:
        from umai.db.session import dispose_engine

        await dispose_engine()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["starter", "usda", "turkomp", "all"], default="starter")
    ap.add_argument(
        "--sleep", type=float, default=3.5, help="seconds between USDA calls; DEMO_KEY needs this"
    )
    ap.add_argument("--turkomp-csv", default="data/turkomp.csv")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
