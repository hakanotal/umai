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
