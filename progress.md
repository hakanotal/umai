# Umai — Progress

One page, three sections. Details live in CLAUDE.md and the module docstrings.

## Done

- **Perception validated** (build-order step 1): all 24 `data/media` photos through the real path, zero parse failures, within-photo variance mean CV 0.07 (gate 0.20). Bias measurement still lacks ground truth (see Todo).
- **Full schema + three migrations**, Postgres 17 + pg_trgm, `alembic` one-shot migrations, pgAdmin on :5050 with the `sql/` queries mounted.
- **Four-stage logging pipeline**: photo → vision (name/state/grams only) → resolver (recipe → library → canonical → provisional) → pure-code arithmetic → one confirmation with per-item gram fixes.
- **Food table**: 46 hand-transcribed USDA Foundation starter rows (Turkish aliases included); four importers (USDA, TurKomp, OpenFoodFacts, label-photo).
- **Self-filling food table** (`core/enrichment.py`): background job researches unmatched names on the coach tier, gated in code by Atwater validation, writes tier-4 rows, backfills waiting items. Verified live (lahmacun 573 kcal where the first session reported 10).
- **Cuisines as perception context** (`/cuisines`, `users.cuisines`): max 6, order-normalised, reaching the vision prompt, the text classifier and the enrichment job.
- **Personal learning layer**: `food_library` and `portion_priors` written after every meal and read by the resolver and the perception prompt; corrections feed the priors.
- **Bot stack live** (`@umai_diet_bot`): text logging (EN/TR), bare-number weigh-ins, photo path with download retry, no transaction held across model calls, allowlist middleware on `dp.update`, evening summary at 21:30 local, enrichment sweep every 20 min, health ingest endpoint (idempotent, bearer auth), webhook + initData HMAC verify for the future Mini App.
- **Model client hardening**: every async call through `acall` (thread off the event loop), per-task timeouts and reasoning effort, usage accounting thread-safe and persisted (`api_usage`), fallback chains across lineages.
- **Calibration engine + simulator**: k/TDEE fit, `reported_intake_target()` as the supported output (k and TDEE individually unidentifiable, documented), safety floors in code. Simulator-tested, deliberately not yet connected to the bot.
- **Correlations module**: pre-declared pairings, Holm correction, 50-seed noise property test.
- **First Docker session audit**: every finding fixed (A1–A6, B1–B8, C1–C6, D1–D9, E1–E4; D10 deliberately unchanged, documented).
- **UI/UX overhaul**: persistent reply keyboard (💧 250/500 ml, 📊 Today, ✏️ Edit today, ⚖️ Weigh in) matched before the intent classifier so button taps never cost a model call; `/help`, `/edit`, `/dinnerware`, `/recipe`, `/library` commands; edit/remove flow for today's entries — per-item gram fixes, whole-meal removal with confirm, water amount edit — with the photo archive and supersede-chain invariants protected; brand palette sampled from the logo (`src/umai/theme.py`) applied to the charts; copy pass (plain language, no em dashes).
- **Photo caption as context**: when a user sends a photo with a caption (e.g. "lahmacun"), the caption is injected into the vision prompt via `PromptContext.note` and into the resolver's tiebreak LLM message as advisory context.
- **Dinnerware calibration** (`/dinnerware`): measured once with a bank card beside the plate, stored per-user, injected into every photo prompt as the primary scale reference. CRUD via `/dinnerware name: description` with inline remove buttons.
- **Recipe creation** (`/recipe`): schema, resolver tier, and compute path all existed; now wired to chat. Name the recipe, add ingredients one per message, optional cooked weight for yield factor, per-100g profile computed and stored.
- **Food library one-tap** (`/library`): surfaces the user's most frequent foods with typical portions for one-tap re-logging.
- **200 tests green** (unit + Postgres integration), ruff + mypy clean, wall-clock ban holds.

## In progress

- **Three weeks of real use** (Phase 1 gate): the bot is running in Docker on the Mac (`docker compose -f docker-compose.app.yml`), logging real meals, enrichment filling gaps as they appear. Pleasant or stop.
- **Health ingest against the real app**: endpoint written, idempotent, tested — but has never received a payload from Health Auto Export (needs the phone + Tailscale).

## Todo

- **Voice notes** — the largest Phase 1 gap; plan calls it the lowest-friction capture method. Needs a transcription model choice (local Whisper vs OpenRouter audio model) and audio download handling.
- **Eval ground truth** — weigh the 20 photos' items so bias (not just variance) can be measured and model changes ranked.
- **USDA full import** — free API key (https://fdc.nal.usda.gov/api-key-signup.html), then `tools/seed_foods.py --source usda`. TurKomp CSV still to be filled (~150–300 dishes).
- **Label-photo and barcode import reachable from chat** — both written, neither wired to a handler.
- **Satiety / alcohol / fasting window / body measurements** — cheap to collect, unlock Phase 4 analysis.
- **Phase 3 analytics wiring** — trend EWMA, calibration, adaptive targets, coaching, check-ins; parked until weeks of history exist.
- **Mini App frontend, weekly review, correlations UI, export, cost reporting** — Phase 4.
- **Pi deploy** when stable (same image, `UMAI_ENV=prod`, webhook + Tailscale); then backups.
- **Stubs**: `tools/replay_health.py`, `tools/benchmark_perception.py`.


---

## Steps: ingest wired end to end (2026-08-22, later)

Everything from the phone to a number on screen, except the phone itself.

**Read path.** `tools.daily_steps` and `tools.steps_by_day` in `core/tools.py`,
grouping by the *local* date through Postgres `timezone()` so a 23:30 Istanbul
sample counts on the day it was walked. Aggregated on the fly rather than
materialised: `daily_rollups` stays unwritten, because backfill — the phone
delivering three days at once — is a non-event for a query over `recorded_at`
and an invalidation problem for a rollup. Two decisions carried in the types:

  * **`None` is not `0`.** No data means "assume a typical day", which is what
    `activity_offset_kcal` does with None; a real zero is about -350 kcal at
    90kg. A failed sync reported as zero would tell the calibration engine the
    user was bedbound.
  * **`DaySteps.samples`** carries the row count, because an export-granularity
    switch (hourly → daily) overwrites the midnight bucket with the whole day's
    total while the other 23 rows survive, doubling the day with nothing in the
    schema able to see it. A run of days reading 24 that suddenly reads 25 is
    that switch, visible in `sql/steps.sql`.

**Surfaced** as one line in `/summary` and the evening push, beside the target
and never folded into it. A suspect day says so instead of printing a number.

**`tools/replay_health.py` is written** (was a 7-line docstring). `--record`
takes one POST on port 8010, saves the body verbatim, prints what the parser
makes of it and exits; `--replay` feeds it through the real ingest path.
Verified end to end against a synthetic payload: recorded, parsed, replayed,
and the second replay wrote 0 and reported 2 duplicates.

**Tests:** 206 green, up from 139. New ones cover multi-sample summation, both
directions of the local-midnight boundary, `None` vs `0`, three-day backfill,
cross-user and cross-metric leakage, aggregate-level idempotency, and a
Europe/London DST transition — the only case that distinguishes a real
tz-database lookup from a fixed offset.

Fixed two pre-existing unscoped assertions in `test_ingest.py` that only passed
against a virgin database; the replay run put real rows in the dev DB and they
failed immediately. Same class as the `test_tools.py` one from the audit.

**Reachability decided, not yet done:** `tailscale serve --set-path
/ingest/health`, leaving both compose files on `127.0.0.1:8000:8000`. The false
"bound to the tailnet interface" comment in `docker-compose.yml` is corrected —
it described a security property the file did not have.

### Still to do, in order

1. Install Tailscale (not present on this Mac) on Mac + phone, enable MagicDNS
   and HTTPS Certificates, run the `serve` command.
2. Install Health Auto Export, configure one REST automation, **steps only,
   hourly aggregation, split-by-source off**. REST export is a paid feature.
3. `just record-health` against the manual export, commit the fixture, then
   re-read the granularity question with a real payload in hand.

On the resilience question: Tailscale means this works from any network, not
only at home, and Health Auto Export pushes on its own schedule. A missed export
heals on the next successful one — the endpoint upserts on a natural key, so a
wide lookback re-sends are counted as duplicates rather than written twice.

### One finding that is not about steps

`users.tz` in the dev database is **`America/New_York`**, not `Europe/Istanbul`.
It was set from the process timezone when the user row was first created. Every
local-day boundary — food totals, `/summary`, the evening summary, and now steps
— is computed from this column, while the scheduler's cron now fires on
`settings.tz` (Europe/Istanbul). The two disagree by seven hours. Nothing in the
step code assumes either; it reads `user.tz`. But the stored value needs to be
whichever is actually right before any of these numbers mean anything.
