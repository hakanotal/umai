#!/usr/bin/env python3
"""Standalone stage-1 perception: photo path in, items/state/grams out.

Build-order step 1 (technical-implementation.md section 13). No database, no
bot, no Telegram. Takes a photo or directory and runs it through the *real*
perception code path -- ModelClient, perception.client.PerceptionClient,
perception.images, perception.prompt, perception.schema -- so the single
riskiest assumption in the project is tested before anything is built on top of
it.

    uv run python tools/perceive.py data/media --out data/perception-results
    uv run python tools/perceive.py IMG_1181.jpeg --repeats 3 --json

The vision model is never asked for calories; if it returns them anyway they are
dropped and the incident is reported, as in the live path.

Priors, recipes and the personal library are empty here: those need the
database, and this script exists to validate perception in isolation. Dinnerware
calibration is loaded because it is the cheapest gram-accuracy win available and
the only prior that does not need history (papers A04: carbohydrate MAPE fell
from 56.6% to 39.5% once physical scale was supplied).

Run a subset with --repeats 3 to estimate within-photo variance, the error the
calibration engine cannot absorb. The full bias/variance split needs the eval/
harness with truth.csv filled for your weighed-meal subset; this script is the
cheaper first look.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from umai.config.models import ModelClient
from umai.config.settings import get_settings
from umai.perception import prompt as prompt_mod
from umai.perception.client import PerceptionClient
from umai.perception.images import taken_at
from umai.perception.schema import PerceptionResult

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# A photo whose weakest item gram-confidence is below this is one the live path
# would ask about before logging. Surfaced in the summary so you can see how
# often that would happen.
CONFIRM_THRESHOLD = 0.75


def load_dinnerware(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read dinnerware at {path}: {exc}", file=sys.stderr)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def local_time_str(when: dt.datetime | None, tz: str) -> str | None:
    """The local-time hint handed to the model. EXIF rarely carries an offset,
    so the caller's timezone is attached here, in one place."""
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when.astimezone(ZoneInfo(tz)).strftime("%A %H:%M")


def _print_human(photo_name: str, rep: int, repeats: int, outcome) -> None:
    r: PerceptionResult = outcome.result
    print(f"  {photo_name} [{rep + 1}/{repeats}]")
    if not r.items:
        print("    (no items detected)")
    for it in r.items:
        ref = f"  [ref: {it.reference_used}]" if it.reference_used else ""
        print(
            f"    {it.name} · {it.state} · {it.grams:g}g  "
            f"(g-conf {it.grams_confidence:.2f}, id-conf {it.identity_confidence:.2f}){ref}"
        )
    if r.clarifying_question:
        print(f"    ? {r.clarifying_question}")
    flags = []
    if outcome.discarded_nutrition:
        flags.append("discarded_nutrition")
    print(
        f"    overall {r.overall_confidence:.2f}  model {outcome.model}  "
        f"{outcome.latency_ms / 1000:.1f}s" + (f"  [{'/'.join(flags)}]" if flags else "")
    )
    print()


def _outcome_json(photo_name: str, rep: int, outcome) -> dict:
    r = outcome.result
    return {
        "photo": photo_name,
        "repeat": rep,
        "model": outcome.model,
        "latency_ms": outcome.latency_ms,
        "overall_confidence": r.overall_confidence,
        "clarifying_question": r.clarifying_question,
        "discarded_nutrition": outcome.discarded_nutrition,
        "prompt_fingerprint": outcome.prompt_fingerprint,
        "items": [
            {
                "name": it.name,
                "state": it.state,
                "grams": it.grams,
                "grams_confidence": it.grams_confidence,
                "identity_confidence": it.identity_confidence,
                "reference_used": it.reference_used,
            }
            for it in r.items
        ],
    }


def _summarise(
    outcomes: list[dict], fails: int, n_photos: int, repeats: int, cost_usd: float, models
) -> None:
    ok = [o for o in outcomes if "error" not in o]
    n_calls = len(outcomes)
    total_items = sum(len(o.get("items", [])) for o in ok)
    g_confs = [it["grams_confidence"] for o in ok for it in o["items"]]
    id_confs = [it["identity_confidence"] for o in ok for it in o["items"]]
    latencies = [o["latency_ms"] for o in ok]
    discarded = sum(1 for o in ok if o["discarded_nutrition"])
    clarifying = sum(1 for o in ok if o["clarifying_question"])

    needing_confirm = 0
    for o in ok:
        items = o["items"]
        weakest = min((it["grams_confidence"] for it in items), default=0.0)
        if weakest < CONFIRM_THRESHOLD or o["overall_confidence"] < CONFIRM_THRESHOLD:
            needing_confirm += 1

    print("=" * 64)
    print("Summary")
    print("=" * 64)
    print(f"  photos: {n_photos}, repeats: {repeats}, calls: {n_calls}, failed: {fails}")
    print(f"  items detected: {total_items}  (mean {total_items / max(len(ok), 1):.1f}/photo)")
    if g_confs:
        print(f"  grams confidence:    mean {mean(g_confs):.2f}")
        print(f"  identity confidence: mean {mean(id_confs):.2f}")
    if latencies:
        print(f"  latency: mean {mean(latencies) / 1000:.1f}s")
    print(
        f"  would-ask-before-logging (weakest g-conf < {CONFIRM_THRESHOLD}): "
        f"{needing_confirm}/{len(ok)} photos"
    )
    print(f"  discarded_nutrition incidents: {discarded}")
    print(f"  clarifying_question raised: {clarifying}")
    if cost_usd > 0:
        toks = models.usage.tokens_in + models.usage.tokens_out
        print(f"  estimated spend: ${cost_usd:.4f}  ({models.usage.calls} calls, {toks:,} tokens)")
    print()
    print("Next: to measure within-photo variance, re-run a subset with --repeats 3.")
    print("For the full bias/variance decision gate, use eval/ with truth.csv filled")
    print("for your weighed-meal subset (eval/README.md).")


def _variance_report(outcomes: list[dict], repeats: int) -> None:
    """Within-photo variance, the one error the calibration engine cannot absorb.

    Total grams per repeat is compared rather than per-item grams, because item
    names drift slightly between repeats ("roasted baby potatoes" vs "baby
    potatoes, roasted") while the total is robust to that. The decision rules
    are the ones eval/README.md states: CV above ~0.20 means the model
    disagrees with itself enough to matter; above ~0.25 means photos cannot be
    the primary logging path.
    """
    if repeats < 2:
        return

    by_photo: dict[str, list[float]] = {}
    for o in outcomes:
        if "error" in o:
            continue
        by_photo.setdefault(o["photo"], []).append(sum(i["grams"] for i in o["items"]))

    cvs = [pstdev(t) / mean(t) for t in by_photo.values() if len(t) >= 2 and mean(t)]

    print()
    print("=" * 64)
    print("Within-photo variance (same photo, same seed, repeats compared)")
    print("=" * 64)
    for name, totals in sorted(by_photo.items()):
        if len(totals) < 2:
            continue
        shown = ", ".join(f"{t:.0f}g" for t in totals)
        cv = pstdev(totals) / mean(totals) if mean(totals) else 0.0
        print(f"  {name:36s} totals {shown}  CV={cv:.2f}")
    if cvs:
        print(f"\n  mean total-grams CV: {mean(cvs):.2f}")
        worst = max(cvs)
        if worst > 0.25:
            print(f"  worst {worst:.2f}: above the restructure threshold (eval/README).")
        elif worst > 0.20:
            print(f"  worst {worst:.2f}: confirm grams before logging, never silent.")
        else:
            print(f"  worst {worst:.2f}: stable. Calibration can absorb the bias.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "path",
        nargs="?",
        default="data/media",
        help="photo file or directory (default: data/media)",
    )
    ap.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="calls per photo; use 3 on a subset for within-photo variance",
    )
    ap.add_argument(
        "--out", default="data/perception-results", help="directory for raw JSON outcomes"
    )
    ap.add_argument(
        "--dinnerware", default="eval/dinnerware.json", help="dinnerware calibration JSON"
    )
    ap.add_argument(
        "--tz", default=None, help="timezone for the local_time hint (default: from settings)"
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="print each outcome as one JSON line instead of human-readable",
    )
    ap.add_argument(
        "--parallel",
        type=int,
        default=6,
        help="concurrent model calls (default: 6; 1 forces sequential)",
    )
    args = ap.parse_args()

    target = Path(args.path)
    if target.is_file():
        photos = [target]
    elif target.is_dir():
        photos = sorted(p for p in target.iterdir() if p.suffix.lower() in PHOTO_EXTS)
        if not photos:
            ap.error(f"no photos in {target}")
    else:
        ap.error(f"{target} is not a file or directory")

    settings = get_settings()
    key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY is unset. Put it in .local.env or export it.")
    tz = args.tz or settings.tz
    dinnerware = load_dinnerware(Path(args.dinnerware))

    models = ModelClient(api_key=key)
    client = PerceptionClient(models)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    if not args.json:
        print(f"Umai perception -- {len(photos)} photo(s), {args.repeats} repeat(s) each")
        print(f"  dinnerware: {len(dinnerware)} item(s) from {Path(args.dinnerware)}")
        print(f"  parallel: {args.parallel} call(s) in flight")
        print(f"  out: {outdir}/")
        print()

    # One prompt context per photo (EXIF + dinnerware), shared by its repeats.
    contexts = {
        photo.name: prompt_mod.PromptContext(
            dinnerware=dinnerware,
            local_time=local_time_str(taken_at(photo), tz),
        )
        for photo in photos
    }

    jobs = [(photo, rep) for photo in photos for rep in range(args.repeats)]
    results: dict[tuple[str, int], object] = {}
    errors: dict[tuple[str, int], Exception] = {}

    if args.parallel <= 1:
        for photo, rep in jobs:
            try:
                results[(photo.name, rep)] = asyncio.run(
                    client.analyse(photo, contexts[photo.name])
                )
            except Exception as exc:  # report, don't abort the batch
                errors[(photo.name, rep)] = exc
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {
                pool.submit(
                    lambda ph=photo: asyncio.run(client.analyse(ph, contexts[ph.name]))
                ): (photo, rep)
                for photo, rep in jobs
            }
            for done, fut in enumerate(as_completed(futures), start=1):
                photo, rep = futures[fut]
                try:
                    results[(photo.name, rep)] = fut.result()
                    note = "ok"
                except Exception as exc:  # report, don't abort the batch
                    errors[(photo.name, rep)] = exc
                    note = f"FAILED: {exc}"
                print(
                    f"[{done}/{len(jobs)}] {photo.name} [{rep + 1}/{args.repeats}] {note}",
                    file=sys.stderr,
                )

    outcomes: list[dict] = []
    fails = 0

    for photo in photos:
        for rep in range(args.repeats):
            if (photo.name, rep) in errors:
                fails += 1
                exc = errors[(photo.name, rep)]
                print(f"  {photo.name} [{rep + 1}/{args.repeats}] FAILED: {exc}", file=sys.stderr)
                outcomes.append({"photo": photo.name, "repeat": rep, "error": str(exc)})
                continue
            outcome = results[(photo.name, rep)]
            record = _outcome_json(photo.name, rep, outcome)
            outcomes.append(record)
            if args.json:
                print(json.dumps(record, ensure_ascii=False))
            else:
                _print_human(photo.name, rep, args.repeats, outcome)

    (outdir / "outcomes.json").write_text(json.dumps(outcomes, indent=2, ensure_ascii=False) + "\n")

    if not args.json:
        _summarise(outcomes, fails, len(photos), args.repeats, models.usage.cost_usd, models)
        _variance_report(outcomes, args.repeats)

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
