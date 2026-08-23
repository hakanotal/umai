# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Umai is a self-hosted personal nutrition assistant (Telegram bot on a Raspberry Pi). Read a
module's docstring before writing into it — each one records the constraint that module exists to
honour.

| File | What it settles |
|---|---|
| `umai-project-plan.md` | Product design. §3 calibration, §4 estimation pipeline, §6.3 data model, §7 roadmap. |
| `technical-implementation.md` | Stack, repo layout, dev loop, testing strategy, Mac→Pi deploy, gotchas. |
| `progress.md` | One-page done / in-progress / todo, kept current. |
| `src/umai/config/models.py` | Model tiers, per-task routing, capability quirks. |
| `papers/README.md` | The 29-paper evidence base, indexed by the design decision each supports. |
| `sql/README.md` | Ready-made inspection queries, mounted into pgAdmin at `/sql`. |

`umai-project-plan.md` references a companion `model-selection.md`, and `papers/README.md`
references `papers/fetch.py`; neither exists. `papers/Insights.md` is empty.

## Current state (2026-08-22)

Built, lint/mypy clean, 200 tests green (unit + Postgres integration): clock, settings, full
schema + two migrations, stage-1 perception (schema/prompt/images/client), stage-2 resolver,
stage-3 compute, four food importers, trend/EWMA, safety rails, calibration, correlations,
health ingest, `tools/simulate.py`, **and the bot stack**: `core/tools.py` (write/read paths),
`core/agent.py`, `telegram/` (aiogram 3.30, allowlist middleware, polling), `web/api.py` (health
ingest + webhook), `scheduler/jobs.py` (evening summary, idempotent), `analytics/charts.py`,
`__main__.py`, `tools/perceive.py`, `tools/seed_foods.py`. `@umai_diet_bot` is live; the foods
table holds the 46-row hand-transcribed USDA starter set.

**The food table fills itself** (`core/enrichment.py`, `enrichment_attempts`): when
the resolver misses, a background job asks the coach-tier model for the
composition per 100g, validates it in code — the load-bearing gate is Atwater,
4P+4C+9F must reconstruct the stated energy — writes a tier-4 row, and backfills
the `food_items` that were waiting. Runs every 20 minutes plus a nudge after a
photo, never on the critical path, frequency-ordered, advisory-locked, three
strikes per name. Verified live: a caption-shaped name resolved to **lahmacun**
(256 kcal/100g) and the meal that once reported "Total 10 kcal" now reports 573.

**Cuisines are perception context, not a preference** (`core/cuisines.py`,
`users.cuisines`, `/cuisines`): the user's traditions reach the vision prompt,
the text classifier and the enrichment job. A model reading a plate cold writes
"flatbread with reddish meat/pepper paste topping", which matches nothing; told
this person eats Turkish food it writes "lahmacun", which is a lookup key. Max 6,
order-normalised on write so the prompt fingerprint stays stable.

**Perception is validated** (build-order step 1): all 24 `data/media` photos ran through the
real path — identification excellent (Turkish dishes named), zero parse failures, zero
discarded nutrition; within-photo variance on a 5×3 subset: mean CV 0.07, worst 0.11 (gate is
0.20, see eval/README). The photo-first design holds.

**Reasoning models, the empty-response trap:** all three configured models are reasoning
models on OpenRouter. Without `reasoning: {"effort": "low"}` in extra_body they exhaust
max_tokens on chain-of-thought and return `content: null`. `config/models.py` handles this;
its max_tokens values include reasoning headroom. Usage accounting is thread-safe (the
perception tool runs calls in parallel).

**USDA importer:** FDC's search endpoint takes `dataType` as *repeated* params, not a
comma-joined string (the joined form 404s). `seed_foods.py --source starter` seeds 46
hand-transcribed Foundation rows so the resolver works before a real USDA key arrives
(DEMO_KEY is throttled to uselessness; free key: https://fdc.nal.usda.gov/api-key-signup.html,
then `--source usda`).

**Steps** land via Health Auto Export over `tailscale serve` (never by widening the container's
loopback bind) and are read per *local* day by `tools.daily_steps` / `tools.steps_by_day`,
aggregated on the fly rather than materialised into `daily_rollups`, which stays unwritten.
`DaySteps.samples` is the tripwire for an export-granularity switch, which would otherwise double
a day silently. **`None` and `0` are different answers**: `calibration.activity_offset_kcal` reads
None as "assume a typical 8,000-step day" and a real zero as about -350 kcal, so a day the phone
failed to sync must never be reported as zero. Steps are shown beside the target and never folded
into it — the plan's standing rule is that activity never adds calories back to the budget.
**Trap for whoever wires steps into calibration:** `current_target` already applies a 1.4 activity
multiplier, so feeding `activity_offset_kcal` in as well counts the same activity twice. Retire
one of the two.

Not yet written: Mini App frontend, `tools/benchmark_perception.py`.
Analytics beyond the static Phase 1 target is deliberately deferred until real history exists
(deployment target is the Pi; calibration needs weeks of data first).

**Deploy note:** the Pi is the target. Dev runs long polling on the Mac; the Pi runs the same
image with `UMAI_ENV=prod` (webhook + Tailscale for the ingest endpoint). Nothing Pi-specific
may enter the code; the only divergences are env vars.

## The calibration finding (read before touching analytics)

The simulator established that **k and TDEE are collinear and individually unidentifiable** over
realistic history. Against a planted k=1.282 / TDEE=2400, a 180-day fit returns k≈1.03 and
TDEE≈2012 — both badly wrong — while the reported-intake target derived from them is accurate to
~1%, because the errors cancel in the direction the target is computed. Cause: over a fortnight
the sum of reported intake barely varies, while the observed balance carries ~0.6 kg (~4600 kcal)
of scale noise. More history does not fix it.

Consequences, all load-bearing:
- `reported_intake_target()` is the supported output. `k_factor` and `tdee_estimate` are internal
  parameters — never show either as a fact about the user's metabolism, never use one without the
  other.
- The confidence intervals are conditional on the prior and are narrower than the truth.
- Score any change to this module on **target error** (`simulate.py hostile`), never on parameter
  recovery. Worst hostile-history target error is currently 8.6%.
- This qualifies the plan's Phase 3 done-criterion ("the system tells you your logging bias factor
  and it is stable"). The stable, reportable quantity is the target, not the factor.

## Commands

Everything is a `just` recipe; `just` alone lists them. `brew install just` if missing.

```bash
just db                 # Postgres 17 + pgvector + pgAdmin in Docker, port 5433 (not 5432, deliberately)
just pgadmin            # browse the data: localhost:5050, no login, server pre-registered
just q sql/today.sql    # or run one of the ready-made queries in the terminal
just q-check            # every sql/ query still parses against the schema
just migrate            # alembic upgrade head
just dev                # the bot: long polling + ingest endpoint, hot reload
just check              # lint + types + test
just t tests/unit/test_compute.py::test_boiled_rice_is_not_raw_rice   # a single test
just record tests/integration/test_perception.py                      # RECORD=1, real paid calls
just check-clock        # enforces the wall-clock ban outside clock.py
just preflight          # live pricing + model capability drift check
just eval MODEL         # the twenty-photo baseline harness
just sim 90 2400 0.78   # plant a known TDEE and bias, check the engine recovers them
just seed               # starter foods into the dev DB
just perceive           # the 24-photo perception run (real API, ~$0.08)
```

Integration tests need Postgres: `UMAI_TEST_DATABASE_URL=postgresql+asyncpg://umai:dev@localhost:5433/umai`,
otherwise testcontainers starts one, otherwise they skip.

`eval/` is a standalone harness with its own conventions, excluded from ruff. `.env` and
`.local.env` hold real API keys — never echo their contents. The keys live in `.local.env`;
the justfile loads it via `set dotenv-path`.

## The architectural invariants

Decisions already taken and defended in the docs. Changing one is a design change, not a refactor.

**The four-stage pipeline.** Photo → (1) vision model returns *items, state, grams, confidence
only* → (2) resolver maps to a `foods` row (recipe → library → canonical → provisional) → (3)
pure-code arithmetic → (4) one confirmation. The vision model is never asked for macros; if it
returns them `_strip_nutrition` drops them. The tiebreak LLM does *judgement* (which row), never
*arithmetic*. **Stage 1's `name` is a lookup key, not a caption** — 1-4 words, no
parentheticals, no positional qualifiers, everything else in `description`. A
descriptive name matches nothing in trigram space and used to log as zero kcal.

**A model number becomes a calorie figure in exactly one place**: `core/enrichment.py`,
behind `validate()`. Nothing else may write a `foods` row from model output, and
nothing it writes is above tier 4.

**Every model call from async code goes through `ModelClient.acall`.** The SDK
client is synchronous; calling it inline stopped the event loop for 17-23s per
photo, during which no other update was served.

**Bias is fine, variance is fatal.** Rank models and estimates on **ratio SD**, not MAPE. Anything
raising run-to-run scatter (non-zero perception temperature, dropped seeds, unstable prompts)
undermines the core feature.

**Macros are derived, never authored.** `food_items` macro columns and recipe per-100g columns are
caches, always recomputable from `food_id` + `grams`. `recipe_ingredients` stores no macros, so
fixing a `foods` row retroactively corrects every recipe.

**Trust tiers 1–4** on every `foods` row (lab → label → user-weighed recipe → model-invented).
Tier 4 is provisional and never silently fact.

**Entries are immutable.** A correction is a new row referencing the original (`superseded_by`),
plus a `corrections` row recording the change.

**Safety rails live in code, not prompts** (`analytics/safety.py`): floor at BMR and never below
~1200/1500 kcal, max ~1% body weight loss per week, protein floor in deficit.

**Statistics are computed in code; the model only narrates.** `correlations.py` tests only
pre-declared pairings, with sample-size, effect-size and Holm-corrected significance gates. The
50-seed noise property test is the guard against invented patterns.

**`core/tools.py` is the only food write path.** Every log (photo, text, correction) lands in
`log_food_items`, where macros are computed and immutability is enforced. The raw-vs-cooked
yield decision is made there, once, where both states are known.

**The allowlist runs before any handler.** `telegram/app.py` middleware; unknown senders are
ignored silently.

## Model configuration

`config/models.py` is the single source of truth: three tiers routed per task via `TASKS`, with
per-task `PARAMS` and `REASONING`. Perception runs `temperature=0, seed=42` deliberately.
Capability flags are load-bearing — `qwen3.7-flash` and `glm-5.3` do **not** support strict
`structured_outputs`, so they get `json_object` mode plus in-code validation. Fallback chains are
drawn from different lineages. All calls set `provider.data_collection: "deny"`. Run `preflight()`
before trusting it.

## Conventions

- Nothing calls `datetime.now()` outside `clock.py`; everything takes a `Clock`. Build
  `tools/simulate.py` targets *before* the analytics they test.
- Tests never hit a model API: cassettes recorded with `RECORD=1`, committed, replayed offline.
- Real Postgres in tests, never SQLite (trigram, native enums, arrays, ON CONFLICT).
- The conftest engine is function-scoped on purpose: pytest-asyncio gives each test its own
  event loop, and asyncpg connections cannot cross loops. Schema creation is session-scoped via
  a one-shot `asyncio.run` engine.
- Timestamps stored UTC with `timezone=True`; convert only at the boundary via `clock.day_bounds`.
- Health ingest is idempotent (natural-key upsert, `xmax` distinguishes insert from update) and
  backfill-tolerant. Payloads must be deduped before `ON CONFLICT` — Postgres cannot update one
  row twice in a statement.
- Phase 1 uses `pg_trgm`, not pgvector. Vector columns, when added, are **512** (CLIP ViT-B/32),
  correcting the `vector(768)` in plan §6.3.
- `food_items.position` preserves display order; fix-buttons address items by it.
- Macro columns on `food_items` are a **cache**: the enrichment backfill rewrites them
  in place from `food_id` + grams without touching entries. That is not a breach of
  immutability, it is the property that makes fixing a `foods` row retroactive.
- No database transaction may be held across a model call. The photo handler reads
  its context, closes, calls, then opens a write transaction.
- Replies are sent with **no parse mode**. A food named "fish & chips" made Telegram
  reject the whole message under HTML.

## Writing style for the docs

Continuous prose with tables and mermaid diagrams, British spelling, no bullet-point padding,
every recommendation carrying its reason and the alternative considered. Match that register.
