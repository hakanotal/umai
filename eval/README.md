# Umai baseline evaluation

Run this before writing any bot code. It answers one question: **can a vision model
estimate portions of *your* food consistently enough for the calibration engine to work?**

It measures two things that look identical in a single run but mean opposite things:

- **Bias** is being consistently wrong in one direction. Bias is fine. The calibration
  engine absorbs it. A model with 30% consistent bias produces a perfectly usable system.
- **Variance** is the same photo returning 450g, 620g and 800g on three calls. Variance
  cannot be calibrated away. If it is high, photos cannot be the primary path and the
  design must lean on recipes and library recall instead.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install openai pillow

export UMAI_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-...
```

The harness fetches live pricing from OpenRouter, so every cost figure in the report is current
rather than copied from a doc. It also warns you before a run if a model lacks vision support or
lacks `structured_outputs`, which saves a wasted twenty photos.

Provider routing is set to `data_collection: "deny"` by default, since these are photos of your
food. Set `UMAI_FALLBACKS` to a comma-separated list to enable OpenRouter fallback routing.

## Collecting the photo set

Twenty photos in `photos/`. The mix matters more than the count.

| Count | Type | Ground truth |
|---|---|---|
| 8 | Meals cooked from ingredients you weighed | Exact. Weigh each ingredient, note the cooked total. |
| 6 | Packaged or labelled items | The label. Weigh the served portion. |
| 6 | Restaurant or unknown meals | None. Tests identification and consistency only. |

Shoot the way you actually would: one photo, normal lighting, from where you sit.
Do not stage them. A benchmark on beautiful photos tells you nothing about Tuesday.

Include your usual plate or bowl in as many as possible, and photograph that plate
once with a bank card beside it. Save the measured diameter into `dinnerware.json`.
That single calibration is the cheapest accuracy win available.

## Ground truth file

Fill in `truth.csv`. One row per item per photo. Leave `grams` blank for the
no-ground-truth photos; identification and consistency are still scored for them.

```csv
photo,item,state,grams,kcal,protein_g,carbs_g,fat_g
01.jpg,chicken breast,grilled,165,272,51.3,0,6.1
01.jpg,white rice,boiled,180,234,4.9,50.8,0.5
02.jpg,lentil soup,boiled,350,,,,
```

## Run

```bash
python run_eval.py --photos photos --truth truth.csv --repeats 3 --out results
```

Add `--model` to benchmark a second model over the same set. Comparing two models on
your own food is worth far more than any published benchmark.

## Reading the output

`results/report.md` gives you the numbers. The decision rules:

| Finding | Meaning | Action |
|---|---|---|
| ratio SD below ~0.20 | Consistent enough | Proceed as planned |
| ratio SD 0.20 to 0.35 | Workable but noisy | Proceed, always confirm grams, never log silently |
| ratio SD above ~0.35 | Too noisy to calibrate | Restructure: recipes and library primary, photos for novel food only |
| within-photo CV above ~0.25 | The model disagrees with itself | Same as above, and consider averaging 2 calls per photo |
| identification accuracy below ~0.80 | It cannot see your food | Check photo conditions first, then try another model |

The single most important number is **ratio SD**, not MAPE. MAPE tells you how wrong it
is. Ratio SD tells you whether the wrongness is fixable.

Two models can post near-identical MAPE and be worlds apart in usefulness. A model that is
consistently 30% low is fine, because the calibration engine multiplies it back up. A model
that averages 15% error by scattering wildly in both directions is not, because there is no
single factor that corrects it. Rank on ratio SD and read MAPE second.
