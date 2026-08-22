#!/usr/bin/env python3
"""
Umai baseline evaluation harness.

Runs each photo through the stage-1 perception schema N times, scores against
ground truth, and reports bias vs variance separately.

The model is asked ONLY for items, state and grams. It is never asked for
calories. That separation is the whole point of the Umai estimation design;
this harness measures whether the one number it does ask for is usable.
"""

import argparse, base64, csv, json, os, re, statistics, sys
from collections import defaultdict
from pathlib import Path

# ----------------------------------------------------------------------------
# Stage 1 schema. Note the absence of kcal/protein/carbs/fat. Deliberate.
# ----------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":                {"type": "string"},
                    "state":               {"type": "string",
                                            "enum": ["raw", "boiled", "grilled", "fried",
                                                     "baked", "roasted", "steamed", "dried",
                                                     "liquid", "unknown"]},
                    "grams":               {"type": "number"},
                    "grams_confidence":    {"type": "number"},
                    "identity_confidence": {"type": "number"},
                    "reference_used":      {"type": "string"},
                },
                "required": ["name", "state", "grams",
                             "grams_confidence", "identity_confidence"],
            },
        },
        "clarifying_question": {"type": ["string", "null"]},
        "overall_confidence":  {"type": "number"},
    },
    "required": ["items", "overall_confidence"],
}

SYSTEM = """You estimate the contents of food photographs.

Report ONLY:
  - what each item is
  - its cooking state
  - its weight in grams (millilitres converted to grams for liquids)
  - your confidence in the identity and, separately, in the weight

Do NOT report calories, protein, carbohydrate or fat. Those are computed
downstream from a food composition table. Reporting them is an error.

Weight is the hard part and the part that matters. Use every scale cue in the
frame: known dinnerware dimensions if provided, cutlery, hands, standard glassware.
State which reference you used. If you have no scale reference, say so in
reference_used and lower grams_confidence accordingly.

Report each distinct component separately rather than the plate as a whole.
Confidence values are 0 to 1 and should be honest, not generous."""


def build_prompt(dinnerware, priors):
    p = ""
    if dinnerware:
        p += "Known dinnerware for this user (use as scale reference):\n"
        for k, v in dinnerware.items():
            p += f"  - {k}: {v}\n"
        p += "\n"
    if priors:
        p += "This user's typical serving sizes:\n"
        for k, v in priors.items():
            p += f"  - {k}: median {v}g\n"
        p += "\n"
    p += "Identify the items in this photo and estimate the weight of each."
    return p


# ----------------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------------

def call_anthropic(model, img_b64, media_type, prompt, temperature):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=2000, temperature=temperature, system=SYSTEM,
        tools=[{"name": "report_items",
                "description": "Report the food items and their weights.",
                "input_schema": SCHEMA}],
        tool_choice={"type": "tool", "name": "report_items"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": prompt}]}])
    for block in resp.content:
        if block.type == "tool_use":
            return block.input, resp.usage.input_tokens, resp.usage.output_tokens
    raise RuntimeError("no tool_use block returned")


def call_openai(model, img_b64, media_type, prompt, temperature):
    from openai import OpenAI
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model, temperature=temperature,
        response_format={"type": "json_schema", "json_schema": {
            "name": "report_items", "schema": SCHEMA, "strict": False}},
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": f"data:{media_type};base64,{img_b64}"}},
                      {"type": "text", "text": prompt}]}])
    u = resp.usage
    return (json.loads(resp.choices[0].message.content),
            u.prompt_tokens, u.completion_tokens)


def call_openrouter(model, img_b64, media_type, prompt, temperature):
    """OpenRouter. Uses the OpenAI-compatible endpoint plus OpenRouter extras:
    provider fallbacks and a deny-training data policy."""
    from openai import OpenAI
    client = OpenAI(base_url="https://openrouter.ai/api/v1",
                    api_key=os.environ["OPENROUTER_API_KEY"])
    extra = {"provider": {"data_collection": "deny"}}
    fallbacks = [m for m in os.environ.get("UMAI_FALLBACKS", "").split(",") if m]
    if fallbacks:
        extra["models"] = fallbacks
        extra["route"] = "fallback"
    kwargs = dict(
        model=model, temperature=temperature, extra_body=extra,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": f"data:{media_type};base64,{img_b64}"}},
                      {"type": "text", "text": prompt}]}])
    if not os.environ.get("UMAI_NO_SCHEMA"):
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "report_items", "schema": SCHEMA, "strict": False}}
    resp = client.chat.completions.create(**kwargs)
    txt = resp.choices[0].message.content
    # some models wrap JSON in a fenced block despite json mode
    m = re.search(r"\{.*\}", txt, re.S)
    parsed = json.loads(m.group(0) if m else txt)
    u = resp.usage
    return parsed, u.prompt_tokens, u.completion_tokens


def openrouter_prices():
    """Live pricing, so cost figures never go stale."""
    import urllib.request
    with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as r:
        data = json.load(r)
    out = {}
    for m in data.get("data", []):
        pr = m.get("pricing", {})
        try:
            out[m["id"]] = {
                "in":  float(pr.get("prompt", 0)) * 1e6,
                "out": float(pr.get("completion", 0)) * 1e6,
                "structured": "structured_outputs" in (m.get("supported_parameters") or []),
                "vision": "image" in (m.get("architecture", {}).get("input_modalities") or []),
            }
        except (TypeError, ValueError):
            continue
    return out


PROVIDERS = {"anthropic": call_anthropic, "openai": call_openai,
             "openrouter": call_openrouter}


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

def norm(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    return " ".join(s.split())


def token_overlap(a, b):
    """Cheap name matcher. Good enough at this scale; replace with embeddings later."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def match_items(predicted, truth, threshold=0.34):
    """Greedy best-overlap matching between predicted and true items."""
    pairs, used = [], set()
    scored = []
    for pi, p in enumerate(predicted):
        for ti, t in enumerate(truth):
            scored.append((token_overlap(p["name"], t["item"]), pi, ti))
    scored.sort(reverse=True)
    seen_p = set()
    for score, pi, ti in scored:
        if score < threshold or pi in seen_p or ti in used:
            continue
        seen_p.add(pi); used.add(ti)
        pairs.append((predicted[pi], truth[ti], score))
    missed = [t for i, t in enumerate(truth) if i not in used]
    spurious = [p for i, p in enumerate(predicted) if i not in seen_p]
    return pairs, missed, spurious


def cv(values):
    """Coefficient of variation. The within-photo consistency measure."""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return None
    m = statistics.mean(vals)
    return statistics.pstdev(vals) / m if m else None


def summarise(runs, truth_by_photo):
    """runs: list of (photo, repeat_idx, parsed_response)"""
    ratios, abs_errs = [], []
    id_hits = id_total = 0
    per_photo_grams = defaultdict(lambda: defaultdict(list))
    rows = []

    for photo, rep, parsed in runs:
        truth = truth_by_photo.get(photo, [])
        pred = parsed.get("items", [])
        pairs, missed, spurious = match_items(pred, truth)

        id_total += len(truth)
        id_hits += len(pairs)

        for p, t, score in pairs:
            per_photo_grams[photo][norm(t["item"])].append(p["grams"])
            tg = t.get("grams")
            if tg:
                ratio = tg / p["grams"] if p["grams"] else None
                if ratio:
                    ratios.append(ratio)
                    abs_errs.append(abs(p["grams"] - tg) / tg)
                    rows.append({"photo": photo, "repeat": rep, "item": t["item"],
                                 "true_g": tg, "pred_g": round(p["grams"], 1),
                                 "ratio": round(ratio, 3),
                                 "pct_err": round(100 * (p["grams"] - tg) / tg, 1),
                                 "grams_conf": p.get("grams_confidence"),
                                 "id_conf": p.get("identity_confidence"),
                                 "ref": p.get("reference_used", "")})

    cvs = [c for photo in per_photo_grams for c in
           [cv(v) for v in per_photo_grams[photo].values()] if c is not None]

    return {
        "n_measurements":   len(ratios),
        "id_accuracy":      id_hits / id_total if id_total else None,
        "mape_grams":       statistics.mean(abs_errs) if abs_errs else None,
        "mean_ratio":       statistics.mean(ratios) if ratios else None,
        "median_ratio":     statistics.median(ratios) if ratios else None,
        "ratio_sd":         statistics.pstdev(ratios) if len(ratios) > 1 else None,
        "mean_signed_err":  statistics.mean([r["pct_err"] for r in rows]) if rows else None,
        "within_photo_cv":  statistics.mean(cvs) if cvs else None,
        "rows":             rows,
    }


def verdict(m):
    out = []
    sd, c, acc = m["ratio_sd"], m["within_photo_cv"], m["id_accuracy"]

    if sd is None:
        out.append("Not enough ground-truth measurements to judge. Weigh more meals.")
    elif sd < 0.20:
        out.append(f"**Ratio SD {sd:.2f}. Consistent.** Proceed as planned. "
                   "Calibration will absorb the bias.")
    elif sd < 0.35:
        out.append(f"**Ratio SD {sd:.2f}. Workable but noisy.** Proceed, but always "
                   "confirm grams before logging. Never log a photo silently.")
    else:
        out.append(f"**Ratio SD {sd:.2f}. Too noisy to calibrate.** Restructure: make "
                   "recipes and library recall the primary path and reserve photo "
                   "estimation for genuinely novel food.")

    if c is not None:
        if c > 0.20:
            out.append(f"**Within-photo CV {c:.2f}. The model disagrees with itself.** "
                       "This is variance, not bias, and calibration cannot fix it. "
                       "Average two calls per photo, or lean harder on recipes.")
        elif c > 0.12:
            out.append(f"Within-photo CV {c:.2f}. Moderate run-to-run drift. Usable, but "
                       "confirm grams rather than logging silently.")
        else:
            out.append(f"Within-photo CV {c:.2f}. Stable across repeats.")

    if acc is not None:
        if acc < 0.80:
            out.append(f"**Identification accuracy {acc:.0%}. Low.** Check lighting and "
                       "framing first, then try a different model before redesigning.")
        else:
            out.append(f"Identification accuracy {acc:.0%}. Fine. As expected, "
                       "recognition is not the bottleneck; portion size is.")

    mr = m["mean_ratio"]
    if mr:
        direction = "under" if mr > 1 else "over"
        out.append(f"Mean true/predicted ratio {mr:.2f}, so the model {direction}estimates "
                   f"by about {abs(1 - 1/mr) * 100:.0f}% on average. Seed the calibration "
                   f"engine's k factor at {mr:.2f} rather than 1.0.")
    return out


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", default="photos")
    ap.add_argument("--truth", default="truth.csv")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", default="results")
    ap.add_argument("--provider", default=os.environ.get("UMAI_PROVIDER", "openrouter"))
    ap.add_argument("--model", default=os.environ.get("UMAI_MODEL"),
                    help="Model id. Comma-separate several to benchmark them head to head.")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--dinnerware", default="dinnerware.json")
    args = ap.parse_args()

    if not args.model:
        sys.exit("Set --model or UMAI_MODEL to the model id you are benchmarking.")
    if args.provider not in PROVIDERS:
        sys.exit(f"Unknown provider. Choose from: {', '.join(PROVIDERS)}")

    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)

    truth_by_photo = defaultdict(list)
    if Path(args.truth).exists():
        with open(args.truth) as f:
            for row in csv.DictReader(f):
                row["grams"] = float(row["grams"]) if row.get("grams") else None
                truth_by_photo[row["photo"]].append(row)
    else:
        print(f"No {args.truth} found. Running identification and consistency only.")

    dinnerware = {}
    if Path(args.dinnerware).exists():
        dinnerware = json.load(open(args.dinnerware))

    photos = sorted(p for p in Path(args.photos).iterdir()
                    if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    if not photos:
        sys.exit(f"No photos found in {args.photos}/")

    fn = PROVIDERS[args.provider]
    prompt = build_prompt(dinnerware, {})
    models = [m.strip() for m in args.model.split(",") if m.strip()]

    prices = {}
    if args.provider == "openrouter":
        try:
            prices = openrouter_prices()
        except Exception as e:
            print(f"Could not fetch live pricing ({e}). Cost figures will be omitted.")

    all_reports, comparison = [], []

    for model in models:
        print(f"\n=== {model} ===")
        info = prices.get(model)
        if info:
            flags = []
            if not info["vision"]:
                flags.append("NO VISION SUPPORT, this will fail")
            if not info["structured"]:
                flags.append("no structured_outputs, expect occasional parse failures")
            print(f"  ${info['in']:.3f}/M in, ${info['out']:.3f}/M out"
                  + ("  [" + "; ".join(flags) + "]" if flags else ""))

        runs, raw, in_tok, out_tok, fails = [], [], 0, 0, 0

        for photo in photos:
            b64 = base64.b64encode(photo.read_bytes()).decode()
            mt = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                  "webp": "image/webp"}[photo.suffix.lower().lstrip(".")]
            for rep in range(args.repeats):
                try:
                    parsed, i, o = fn(model, b64, mt, prompt, args.temperature)
                    in_tok += i; out_tok += o
                    runs.append((photo.name, rep, parsed))
                    raw.append({"model": model, "photo": photo.name,
                                "repeat": rep, "response": parsed})
                    names = ", ".join(f"{x['name']} {x['grams']:.0f}g"
                                      for x in parsed.get("items", []))
                    print(f"  {photo.name} [{rep+1}/{args.repeats}] {names}")
                except Exception as e:
                    fails += 1
                    print(f"  {photo.name} [{rep+1}] FAILED: {e}", file=sys.stderr)

        slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
        (outdir / f"raw-{slug}.json").write_text(json.dumps(raw, indent=2))

        m = summarise(runs, truth_by_photo)
        if m["rows"]:
            with open(outdir / f"measurements-{slug}.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(m["rows"][0].keys()))
                w.writeheader(); w.writerows(m["rows"])

        cost = None
        if info:
            cost = (in_tok * info["in"] + out_tok * info["out"]) / 1e6

        comparison.append({"model": model, **{k: m[k] for k in
                          ("id_accuracy", "mape_grams", "mean_ratio",
                           "ratio_sd", "within_photo_cv", "n_measurements")},
                          "fails": fails, "cost": cost,
                          "cost_per_photo": (cost / (len(photos) * args.repeats))
                                            if cost else None})

        all_reports.append((model, m, in_tok, out_tok, cost, fails))

    # ---------------- report ----------------
    def fmt(v, pct=False, dp=3):
        if v is None: return "n/a"
        return f"{v:.0%}" if pct else f"{v:.{dp}f}"

    lines = ["# Umai baseline evaluation", "",
             f"Provider `{args.provider}`, temperature {args.temperature}, "
             f"{len(photos)} photos, {args.repeats} repeats each.", ""]

    if len(models) > 1:
        lines += ["## Head to head", "",
                  "Rank on **ratio SD**, not MAPE. Consistent bias is absorbed by the "
                  "calibration engine; scatter is not.", "",
                  "| Model | Ratio SD | Within-photo CV | ID acc | MAPE g | Mean ratio | Fails | $/photo | Run cost |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c in sorted(comparison, key=lambda x: (x["ratio_sd"] is None, x["ratio_sd"] or 9)):
            lines.append(
                f"| `{c['model']}` | **{fmt(c['ratio_sd'])}** | {fmt(c['within_photo_cv'])} | "
                f"{fmt(c['id_accuracy'], True)} | {fmt(c['mape_grams'], True)} | "
                f"{fmt(c['mean_ratio'])} | {c['fails']} | "
                f"{'$'+format(c['cost_per_photo'],'.5f') if c['cost_per_photo'] else 'n/a'} | "
                f"{'$'+format(c['cost'],'.4f') if c['cost'] else 'n/a'} |")
        lines.append("")

    for model, m, in_tok, out_tok, cost, fails in all_reports:
        lines += [f"## {model}", "",
                  f"Tokens: {in_tok:,} in, {out_tok:,} out."
                  + (f" Run cost ${cost:.4f}." if cost else "")
                  + (f" {fails} failed calls." if fails else ""), "",
                  "| Metric | Value | What it tells you |", "|---|---|---|",
                  f"| Identification accuracy | {fmt(m['id_accuracy'], True)} | Whether it sees your food at all |",
                  f"| MAPE on grams | {fmt(m['mape_grams'], True)} | How wrong it is |",
                  f"| Mean signed error | {fmt(m['mean_signed_err'], dp=1)}% | Which direction it is wrong in |",
                  f"| Mean true/predicted ratio | {fmt(m['mean_ratio'])} | Seed value for the calibration k factor |",
                  f"| **Ratio SD** | **{fmt(m['ratio_sd'])}** | **Whether calibration can fix it. The number that matters.** |",
                  f"| Within-photo CV | {fmt(m['within_photo_cv'])} | Whether the model agrees with itself |",
                  f"| Measurements | {m['n_measurements']} | Sample size |",
                  "", "### Verdict", ""] + [f"- {l}" for l in verdict(m)] + [""]

    (outdir / "report.md").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))
    print(f"\nWritten to {outdir}/report.md")


if __name__ == "__main__":
    main()
