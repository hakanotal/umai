# Umai — Progress

One page, three sections. Details live in CLAUDE.md and the module docstrings.

## Done

- **Perception validated** (build-order step 1): all 24 `eval/photos` photos through the real path, zero parse failures, within-photo variance mean CV 0.07 (gate ~0.25, see `eval/README.md`). Bias measurement still lacks ground truth (see Todo).
- **Full schema + four migrations**, Postgres 17 + pg_trgm, `alembic` one-shot migrations, pgAdmin on :5050 with the `sql/` queries mounted.
- **Four-stage logging pipeline**: photo → vision (name/state/grams only) → resolver (recipe → library → canonical → provisional) → pure-code arithmetic → one confirmation with per-item gram fixes.
- **Food table**: 46 hand-transcribed USDA Foundation starter rows (Turkish aliases included); four importers (USDA, TurKomp, OpenFoodFacts, label-photo).
- **Self-filling food table** (`core/enrichment.py`): background job researches unmatched names on the coach tier, gated in code by Atwater validation, writes tier-4 rows, backfills waiting items. Verified live (lahmacun 573 kcal where the first session reported 10).
- **FNDDS** (`core/fndds.py`, `tools/seed_fndds.py`, `just extract-fndds`): the USDA Food and Nutrient Database for Dietary Studies, seeded from a CSV bundle into `fndds_foods`, exposed to the enrichment model as the `fndds_search` tool (`core/enrichment.py:205`). Not in the synchronous resolver path; only populates `foods` after the fact.
- **Cuisines as perception context** (`/cuisines`, `users.cuisines`): max 6, order-normalised, reaching the vision prompt, the text classifier and the enrichment job.
- **Personal learning layer**: `food_library` and `portion_priors` written after every meal and read by the resolver and the perception prompt; corrections feed the priors.
- **Bot stack live** (`@umai_diet_bot`): text logging (EN/TR), bare-number weigh-ins, photo path with download retry, no transaction held across model calls, allowlist middleware on `dp.update`, evening summary at 21:30 local, enrichment sweep every 20 min, health ingest endpoint (idempotent, bearer auth), webhook + initData HMAC verify for the future Mini App.
- **Model client hardening**: every async call through `acall` (thread off the event loop), per-task timeouts and reasoning effort, usage accounting thread-safe and persisted (`api_usage`), fallback chains across lineages.
- **Calibration engine + simulator**: k/TDEE fit, `reported_intake_target()` as the supported output (k and TDEE individually unidentifiable, documented), safety floors in code. Simulator-tested, deliberately not yet connected to the bot.
- **Correlations module**: pre-declared pairings, Holm correction, 50-seed noise property test.
- **First Docker session audit**: every finding fixed (A1–A6, B1–B8, C1–C6, D1–D9, E1–E4; D10 deliberately unchanged, documented).
- **UI/UX overhaul**: persistent reply keyboard (💧 250 ml, 📊 Today, 📊 Week, ✏️ Edit, 📚 Library, 🍽️ Dinnerware, ⚖️ Weigh in, 📝 Recipe, 🌍 Cuisines) — every feature reachable without typing; button taps matched before the intent classifier so they never cost a model call; `/help`, `/edit`, `/dinnerware`, `/recipe`, `/library` commands; edit/remove flow for today's entries — per-item gram fixes, whole-meal removal with confirm, water amount edit — with the photo archive and supersede-chain invariants protected; brand palette sampled from the logo (`src/umai/theme.py`) applied to the charts; copy pass (plain language, no em dashes).
- **Photo caption as context**: when a user sends a photo with a caption (e.g. "lahmacun"), the caption is injected into the vision prompt via `PromptContext.note` and into the resolver's tiebreak LLM message as advisory context.
- **Dinnerware calibration** (`/dinnerware`): measured once with a bank card beside the plate, stored per-user, injected into every photo prompt as the primary scale reference. CRUD via `/dinnerware name: description` with inline remove buttons.
- **Recipe creation** (`/recipe`): schema, resolver tier, and compute path all existed; now wired to chat. Name the recipe, add ingredients one per message, optional cooked weight for yield factor, per-100g profile computed and stored.
- **Food library one-tap** (`/library`): surfaces the user's most frequent foods with typical portions for one-tap re-logging.
- **212 tests green** (unit + Postgres integration), ruff + mypy clean, wall-clock ban holds.
- **Repo cleanup**: integration tests moved out of `tests/fixtures/integration/` to
  `tests/integration/` where every doc already said they were; docs collected under `docs/`;
  empty stub packages (`core/prompts/`, `tests/sim/`) and the docstring-only
  `tools/benchmark_perception.py` removed; `telegram/handlers/` split from one 1,154-line
  module into one router per feature with the include order documented as load-bearing;
  the `papers/` ignore rule unanchored so 23MB of licensed PDFs cannot be committed by a move.

## In progress

- **Three weeks of real use** (Phase 1 gate): the bot is running in Docker on the Mac (`docker compose -f docker-compose.app.yml`), logging real meals, enrichment filling gaps as they appear. Pleasant or stop.
- **Health ingest against the real app**: endpoint written, idempotent, tested — but has never received a payload from Health Auto Export (needs the phone + Tailscale).

## Todo

### Roadmap gates

The plan's §10.6 says "Then Phase 1, and use it for three weeks before writing a line of Phase 2." Phase 2 and 3 machinery was built ahead of the Phase -1/0/1 gates:

- **Phase -1 (baseline eval)** — *half met.* Variance measured (mean CV 0.07). Bias never measured: `eval/photos/` holds 24 photos but `eval/truth.csv` has 3 rows copied from the README example. The plan calls this "run before any bot code"; the bot is written.
- **Phase 0 (passive data for three days)** — *not met.* No real Health Auto Export payload has ever arrived. Blocked on Tailscale + the paid REST export.
- **Phase 0.5 (food table)** — *partly.* 46 starter rows against the plan's 150–300; the label-photo importer (named as the cheapest way to fill every gap thereafter) is written but unwired.
- **Phase 1 (the logging loop)** — *built except voice.* The three-week use gate has not started.

### Built but not reachable

A large amount of code is written, migrated, and in some cases model-routed — but nothing calls it:

| Capability | State | Evidence |
|---|---|---|
| Label-photo import | Written, no caller | `resolver/importers/label_photo.py:65` never imported; the only `F.photo` handler (`handlers/photo.py:48`) routes unconditionally to perception. A fully configured `label_ocr` model tier (`config/models.py:100`) has no caller |
| Barcode / OpenFoodFacts | Written, no caller | `openfoodfacts.py:83` has zero importers |
| Calibration, trend, correlations | Written, no caller | No import from any handler, `agent.py`, `tools.py` or `jobs.py`. `CalibrationState` and `TrendWeight` tables are migrated but never read or written |
| Mini App | Auth helper only | `verify_init_data` (`web/api.py:106`) never called; `web/static/` holds a 0-byte `.gitkeep` |
| Voice | Not written | No `F.voice` handler, no transcription task in `TASKS`. `EntrySource.voice` is a dead enum member |
| Scheduled jobs | 2 of 6 | `jobs.py:1` promises evening check-in, weekly review, biweekly recalibration and backups; only `evening_summary` and `enrichment_sweep` are registered |
| Command discoverability | 9 of 10 | `app.py:29-38` publishes `start, help, summary, week, edit, library, dinnerware, recipe, cuisines`; all reachable from the reply keyboard; only `/today` is an alias for `/summary` |

### Recommended next step

Wire what is already written, **label-photo first**. It is the cheapest accuracy win available: the food table is at 46 rows against a 150–300 target, and label-photo is Phase 0.5's designated tool for filling every gap. The code and its `label_ocr` model tier already exist, and it needs no new dependency. Barcode follows the same path.

What is deliberately *not* next: calibration wiring waits for weeks of real history **and** for the double-count (`* 1.4`) to be retired; voice is the remaining Phase 1 gap but its transcription approach is undecided; the Mini App is Phase 4.

### Remaining items

- **Eval ground truth** — weigh the 20 photos' items so bias (not just variance) can be measured and model changes ranked.
- **USDA full import** — free API key (https://fdc.nal.usda.gov/api-key-signup.html), then `tools/seed_foods.py --source usda`. TurKomp CSV still to be filled (~150–300 dishes).
- **Satiety / alcohol / fasting window / body measurements** — cheap to collect, unlock Phase 4 analysis.
- **Pi deploy** when stable (same image, `UMAI_ENV=prod`, webhook + Tailscale); then backups.


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

**Tests:** 212 green, up from 139. New ones cover multi-sample summation, both
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

`TZ=America/New_York` in `.local.env`; the running `umai-app-1` container has inherited it
(`docker inspect` confirms), and `users.tz` in the dev DB is the same. Istanbul is +7.
Every local-day boundary — food totals, `/summary`, the evening summary and steps — is
computed from `settings.tz` (`core/tools.py:1217`), so today's numbers land on the wrong day
and the 21:30 summary fires at 14:30 local. The code is correct; the configuration is wrong.
Fixing `.local.env` corrects new rows; the existing `users.tz` row needs a separate update,
and there is no in-app way to change a timezone.

---

## Timezone made single-source, and steps in /week (2026-08-23)

**A live bug, found by inspection.** The container was running on
`Europe/Istanbul` while `.local.env` said `America/New_York`, so the evening
summary was scheduled for 14:30 local. Cause: a compose `environment:` entry
overrides `env_file:`, and `${TZ:-Europe/Istanbul}` interpolates from the
*shell* (unset), not from the env file — so a hardcoded fallback silently beat
the configured value.

Four static timezones removed:

  * both compose files — the `TZ:` override is gone entirely, so `env_file` is
    the single source and cannot be shadowed;
  * `config/settings.py` — no default city. This was exactly the
    "plausible-looking wrong default" the person fields three lines below
    refuse to have: it seeds every user row and every day boundary and looks
    right until a day lands on the wrong date. `check_startup()` now refuses to
    boot without `TZ`, and a validator rejects a name the tz database does not
    know, at load rather than inside a handler hours later;
  * `db/models.py` — `User.tz` had the same hardcoded default;
  * `scheduler/jobs.py` — the real inconsistency. The job's *contents* were
    computed in `user.tz` while its *firing time* used `settings.tz`. New
    `scheduling_tz()` reads the user row, falls back to config only before a
    user exists, and warns when the two disagree.

Verified in the rebuilt container: settings, user row and cron all read
`America/New_York`; next fire 21:30-04:00. Six regression tests in
`tests/unit/test_settings.py`, including one asserting there is no default city.

**No data migration was needed, and that is worth recording.** Every timestamp
column is `timestamp with time zone`, so stored values are absolute instants;
the zone only decides how they are bucketed at read time. Changing it changes
interpretation, never stored truth.

**Data actually repaired** (dump taken first):

  * The zero-gram phantom item on the lahmacun entry — the user had corrected it
    to 0g, which means "not mine", and the fixed correction path now drops such
    an item. Positions renumbered contiguously.
  * One `media` row linked to the entry it produced, where `created_at` matched
    `logged_at` to the millisecond. The other three unlinked media have no
    surviving entry and were left alone rather than guessed at.

Two items on that meal stay unmatched on purpose: their *names* are captions
from the old perception schema ("fresh parsley / cilantro (garnish on
flatbread)"), the enrichment job has correctly used all three attempts on them,
and rewriting a `detected_name` would falsify the perception record for about
12 kcal of garnish. New photos cannot produce such names.

**Steps in `/week`.** Listed per day beside intake — never netted against it,
per the standing rule that activity does not add calories back — with a total
and a per-day average over *the days the phone reported*, not over seven, since
dividing by a day that never synced would report a fall in activity that did
not happen. A day with steps but no food is still listed: a gap in logging is
not a gap in living. `/summary` already carried the line.

Real output: six days of backfill, 21,956 steps, 3,659/day.
