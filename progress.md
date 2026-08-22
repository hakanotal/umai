# Umai — Progress

## Done
- **Test infrastructure**: fixed the asyncpg cross-event-loop conftest bug (function-scoped engine, session-scoped schema via one-shot `asyncio.run`). 115 tests green. Also fixed the pre-existing `row.inserted` bug in `ingest/health.py` (asyncpg doesn't expose `text()` aliases as Row attributes).
- **Env split**: justfile now loads `.local.env` (`dotenv-path`); Settings always read both files.
- **Perception validated (build-order step 1)**: `tools/perceive.py` over all 24 photos in `data/media` — identification excellent (Turkish dishes named: turşu, etli pilav, choban salatasi), 0 parse failures, 0 discarded nutrition. Within-photo variance on a 5-photo × 3-repeat subset: mean total-grams CV 0.07, worst 0.11 — well under the 0.20 "proceed as planned" gate. Bias is calibratable; the photo-first design holds.
- **Reasoning-model fix**: all three configured models are reasoning models on OpenRouter; they exhausted `max_tokens` on chain-of-thought and returned empty strings. `config/models.py` now sets `reasoning: {"effort": "low"}` per task (with usage-accounting lock for parallel calls) and raised max_tokens (perception 4000).
- **Improved perception system prompt** (`perception/prompt.py`): structured field guidance, documented bias corrections (underestimation of large portions, oil/sauce, compacted grains, Turkish glassware), Turkish-dish naming, explicit raw-vs-cooked explanation.
- **Bot stack built and smoke-tested end to end** (build-order step 4): `core/tools.py` (the only food write path: derived macros, immutable entries, yield-factor raw→cooked, corrections as supersede+Correction rows, day totals, static Mifflin-St Jeor target with safety floors), `core/agent.py` (intent routing + text food logging via one cheap structured call; bare numbers 30–250 auto-read as weigh-ins), `telegram/` (aiogram 3.30: allowlist middleware, /start /summary /week, quick-action buttons, photo pipeline with download retry, per-item gram fix FSM), `web/api.py` (health ingest with bearer auth + telegram webhook route + initData HMAC verify for the future Mini App), `scheduler/jobs.py` (evening summary at 21:30, idempotent via `job_runs` claim table), `analytics/charts.py` (matplotlib Agg PNGs), `__main__.py` (composition root: polling in dev / webhook in prod, APScheduler, uvicorn, api_usage persistence).
- **Smoke test results** (through the same code the handlers call, live DB + live models): English text log ✓, Turkish text log ✓ ("iki yumurta ve 100 gram beyaz peynir" → egg + white cheese with correct macros), water ✓, bare-number weigh-in with trend arrow ✓, photo log ✓ (salmon 180g → 371 kcal exactly from the foods row; "roasted baby potatoes" honestly flagged unmatched), /summary with target/budget/protein ✓. 9 model calls = $0.0062, on the $1.36/month estimate. HTTP /healthz ✓, ingest 401s without token ✓, long polling boots ✓. Smoke-test rows cleaned from the DB afterwards.
- **Schema**: second migration — `food_items.position` (stable display order for fix buttons), `job_runs` (idempotent scheduling), unique dinnerware per user.
- **Foods seeded**: fixed USDA importer (FDC API takes repeated `dataType` params, not comma-joined — the joined form 404s); 46 hand-transcribed USDA Foundation rows (`--source starter`) covering the staples with Turkish aliases ("tavuk gogus"→chicken breast resolves exact). USDA live import needs a real API key (DEMO_KEY is throttled to uselessness).
- **Telegram**: `@umai_diet_bot` live, token + allowlist (6783665250) + person fields (male/178cm/2000-10-04/start 89.9kg) in `.local.env`. Verified via getMe.

## In progress
- **First live use, now in Docker.** `docker compose -f docker-compose.app.yml up -d` runs the
  app container (`umai:local`, built from the Dockerfile exactly as the Pi will) against the dev
  Postgres, with `.local.env` injected and `DATABASE_URL` repointed at `host.docker.internal`.
  `@umai_diet_bot` is polling and healthy; the container sees the same data as the host run
  (46 foods, onboarding weight 89.9). Recipes: `just docker-app` / `docker-app-logs` /
  `docker-app-down` / `docker-migrate`. Caught one works-on-host-fails-in-container bug in the
  process (Dockerfile was missing `README.md`, which pyproject declares).

## Todo
- Get a free USDA API key (https://fdc.nal.usda.gov/api-key-signup.html), set `USDA_API_KEY` in `.local.env`, run `uv run python tools/seed_foods.py --source usda` for the full 64-query import.
- TurKomp subset: fill `data/turkomp.csv` (~150–300 Turkish dishes) per `resolver/importers/turkomp.py` column format.
- Three weeks of real use (Phase 1 gate): pleasant or stop.
- Pi deploy when stable: `docker compose` on the Pi (arm64, same image), webhook mode + Tailscale for Health Auto Export. Analytics (calibration, correlations, coach narration) deliberately deferred until real history exists.
- Remaining stubs: `tools/replay_health.py`, `tools/benchmark_perception.py`, Mini App frontend.
- CLAUDE.md current-state section kept in sync (done this session).

## Notes
- Repo has **no git commits yet** — everything is untracked. Commit when you're ready.
- Health ingest endpoint ready but untested against the real Health Auto Export app (needs the phone + Tailscale).
- CLAUDE.md vanished from disk mid-session (second time); recreated with current state.
- `just` is not installed on the Mac yet: `brew install just`. All recipes work as plain commands meanwhile (`uv run python -m umai`, etc.).

---

## Audit of the first Docker session (2026-08-22)

Source material: `docker logs umai-app-1` (40 lines, one live session), the dev
Postgres after that session (10 log entries, 19 food items, 2 corrections, 2 media
rows), and a read of every module in the write and reply path. Ordered by what it
costs, not by where it sits in the tree. **Everything in A, B, C and E below is
now fixed** (see "Audit fixes" further down for what each one became); D is fixed
except D10, which is a deliberate non-change. The findings are kept in full
because the reasoning behind each fix is the finding.

### A. Broken now — these fail on the running container

**A1. The evening summary can never run.** `__main__.py:26` does
`from umai.db.session import _factory`, which binds the name to `None` at import
time; `init_engine` rebinds the *module* global, not the copy, so
`schedule_jobs(scheduler, _factory, …)` at line 92 hands the job a `None` factory
and 21:30 raises `TypeError: 'NoneType' object is not callable`. Verified directly:
after `init_engine`, `session._factory` is a factory and the imported name is still
`None`. Import the module and reach through it (`session._factory`), or better, have
`init_engine` return the factory. `job_runs` being empty is consistent with this.

**A2. Model spend is never recorded.** `config/models.py:257` calls `self._on_usage(…)`
without awaiting it, and `_on_usage` is the coroutine function `_persist_usage`. The
log shows `RuntimeWarning: coroutine '_persist_usage' was never awaited`, and
`api_usage` holds 0 rows after a session of ~10 paid calls. `_account` also runs on
worker threads (the perception tool parallelises), so the fix is to capture the loop
at construction and `asyncio.run_coroutine_threadsafe`, not a bare `create_task`.

**A3. Every model call blocks the whole bot.** `ModelClient.call` is the *synchronous*
OpenAI client, and it is invoked straight from `async def` handlers
(`handlers/__init__.py:248` perception, `agent.py:163` routing, `match.py:236`
tiebreak). The event loop stops for the duration. The logs show it plainly: update
289644810 took 17 742 ms and 289644821 took 23 575 ms, during which nothing else —
not a water tap, not `/summary` — could be served. Wrap each call in
`asyncio.to_thread`, or move to `AsyncOpenAI`.

**A4. No timeout on the model client.** `OpenAI(base_url=…, api_key=…)` takes the SDK
default (10 minutes). Combined with A3, one hung OpenRouter request freezes the bot
for ten minutes with no log line. Set an explicit `timeout=` (60s for perception,
20s for utility) and a `max_retries`.

**A5. The scheduler's cron is in the wrong timezone.** `jobs.py:116` adds a cron
trigger with no `timezone=`, so APScheduler uses the process timezone. The container
sets no `TZ`, so that is UTC, while `settings.tz` defaults to `Europe/Istanbul`. The
"21:30 local" summary would fire at 00:30 local. The docstring at `jobs.py:105`
claims the shift happens; it does not. Pass `timezone=ZoneInfo(settings.tz)`.

**A6. Replies are sent as HTML with unescaped model text.** `app.py:76` sets
`parse_mode=ParseMode.HTML` globally, and no reply escapes anything. A detected name
containing `&`, `<` or `>` — "fish & chips", "chicken <br>east" from a sloppy model —
makes Telegram reject the whole message with 400 "can't parse entities", so the meal
is written to the DB and the user is told nothing. Either escape in `format_meal` /
`format_day` or drop the default parse mode; nothing in the Phase 1 replies needs
HTML.

### B. Data the system is silently losing

**B1. Unresolved items are logged as zero calories, and the total is shown as fact.**
This is the most damaging behaviour in the session. Of 19 food items, 12 resolved to
`method='new'`, `food_id NULL`, `kcal=0`. The pide photo logged six items totalling
340 g of food and reported **10 kcal** — the only matched row being the 25 g of
onion. A typed "brisket, 200 g" logged 0 kcal. `format_meal` (`tools.py:475`) still
prints `Total 10 kcal`, and `day_totals` folds that zero into the day and compares it
to the target. The per-item `⚠ not in food table` flag is not enough: the *total* is
the number the user reads, and it is wrong by an order of magnitude in a consistent
direction. Options, in order of preference: (a) call the already-written
`Resolver.create_provisional` (see B2) so a tier-4 row carries a real estimate;
(b) suppress the total entirely when any item is unmatched, rather than printing a
number that is arithmetically correct and factually absurd.

**B2. `Resolver.create_provisional` is dead code.** `match.py:298` implements the
tier-4 provisional path the architecture calls for and *nothing in the repository
calls it* (grep across `src/`, `tests/`, `tools/` returns only the definition). The
resolver's fourth tier does not exist at runtime; the resolver has three tiers and a
hole.

**B3. The vision model's names are unusable as lookup keys.** Perception returns
`"flatbread with reddish meat/pepper paste topping (turkish pide-style flatbread)"`
and `"fresh parsley / cilantro (garnish on flatbread)"`. `pg_trgm` similarity of a
78-character descriptive phrase against `"wheat flour"` is near zero, so the resolver
cannot match even foods that *are* in the table. The prompt (`perception/prompt.py:30`)
asks for specificity and gets prose. The schema should carry two fields — a short
canonical `name` (two or three words, no parentheticals, no positional qualifiers) and
a free-text `description` — with only the former reaching the resolver. This single
change is probably worth more than everything else in section B.

**B4. Photos are never linked to the entry they produced.** `handlers/__init__.py:254`
constructs `Media(...)` with no `entry_id`, before `log_photo` has run and produced
one. Both media rows in the DB have `entry_id NULL`, so no logged meal can be traced
back to its photo — which breaks re-scoring old photos with a better model, the stated
reason for storing them. There is also no dedup: `ix_media_sha256` is not unique, so
re-sending the same photo writes a second row (the file itself is deduped by
`_download_with_retry`).

**B5. `perception_runs` is never written.** The table is empty after two photo logs.
`prompt.fingerprint` exists specifically so that "did the prompt change or did the
model?" is answerable after the fact (`prompt.py:139`), and `PerceptionOutcome`
carries `model`, `latency_ms`, `raw` and `prompt_fingerprint` all the way to the
handler, where they are dropped. The raw response is also thrown away, so old photos
cannot be re-scored offline.

**B6. The personal library never learns.** `FoodLibrary` is read by
`Resolver._match_library` (`match.py:149`) and written by nothing. `times_logged` is
never incremented, no row is ever inserted; the table is empty after 10 entries.
Resolution tier 2 is permanently inert, which means the system cannot get better at
recognising the food you actually eat — one of the plan's central promises.

**B7. Portion priors are never gathered or used.** `PromptContext.portion_priors` is
described in `prompt.py:111` as "the mechanism by which the system's largest error
shrinks with use". The photo handler builds its context with `dinnerware` and
`local_time` only (`handlers/__init__.py:242`), and nothing writes `portion_priors`.
The two grams corrections made in this session (120→0, 170→150) taught the system
nothing. `Correction.applied_to_prior` is `false` on both rows and no code ever sets
it true.

**B8. Absorbed frying oil is never counted.** `compute()` supports
`apply_fat_absorption`, and the module docstring calls it "exactly the thing you most
want to track" — `_macros_for` (`tools.py:212`) never passes it. Same for the `ml`
path: drinks logged in millilitres never go through `density_g_per_ml`.

### C. Correction flow

**C1. A second correction to the same meal is impossible.** The confirmation keyboard
is built from the entry id at send time (`keyboards.py:28`). The first fix supersedes
that entry, `_entry_by_prefix` filters `superseded_by IS NULL`
(`handlers/__init__.py:187`), and the buttons still under the old message now resolve
to nothing — the user is told "that meal is too old to edit". After a fix the bot
should re-send the meal with a fresh keyboard pointing at the new entry.

**C2. "Fixed." is reported even when nothing was fixed.** `_fix_item_grams` silently
does nothing when `item_no` is out of range, and `supersede_with_grams` returns `None`
when the entry is already superseded; `number_received:180` answers "Fixed." either
way.

**C3. The correction rewrites `grams_source` for every item.**
`supersede_with_grams:255` sets `grams_source=GramsSource.user, grams_confidence=1.0`
on *all* items in the new entry, not just the corrected one. In the DB, entry
`ccbfaec2` shows all six items as `user`-sourced with full confidence when the user
only touched item 5. Any future calibration that weights user-weighed grams above
model-estimated ones will be reading a fabricated provenance.

**C4. Setting an item to 0 g leaves a zero-gram phantom.** The session's first
correction was 120 g → 0 g — plainly "that item is not mine". It survives as a 0 g row
in the new entry. The gram-fix flow needs a delete, or 0 should drop the item.

**C5. The `Awaiting.number` state is never cleared by other paths.** A photo or a
command sent while the bot is waiting for a number leaves the state set; the next
plain text is then swallowed by `number_received` instead of reaching the food-logging
path.

**C6. `_entry_by_prefix` is unscoped.** It scans the 50 most recent entries across
*all* users and *all* kinds, matching on an 8-hex-character prefix. Single-user today,
but it should filter `user_id` and `kind == food`.

### D. Correctness and provenance, smaller

**D1. Typed food claims to be model-estimated.** `agent._log_food:232` hard-codes
`grams_source=GramsSource.vlm` for text logs. The DB shows the typed "brisket" entry
as `vlm`. The classifier already returns `grams_estimated`; that is the flag that
should choose between a user-stated and an estimated source, and no text log is ever
`vlm`.

**D2. `log_water` / `log_weight` fall through on a zero.** `agent.py:175` and `:186`
guard with `and c.ml` / `and c.kg`, so a classification of 0 lands in the "I didn't
catch that" branch rather than being rejected on its own terms.

**D3. `occurred_at` and `logged_at` disagree by the length of the API call.**
Entry `1178ab92` has `logged_at 16:06:57` *before* `occurred_at 16:07:12`.
`logged_at` defaults to Postgres `now()`, which is transaction-start time, and the
transaction is opened before the ~15-second perception call. Which leads to:

**D4. A database transaction is held open across the whole vision call.** The photo
handler opens `session_scope()` at `handlers/__init__.py:239` and does not close it
until after perception, resolution (which itself makes model calls) and the reply.
That is a 17–23-second write transaction and a pool connection held for the duration,
on a 5-connection pool. Do the network work first, open the transaction to write.

**D5. The "Looking…" message leaks on every failure path.** It is deleted only at the
end of the happy path (`handlers/__init__.py:276`); the three early `return`s — empty
photo, download failure, perception exception — leave it in the chat forever.

**D6. `/fix` and `/addfood` are advertised and do not exist.** `agent.py:245` tells the
user to "correct with /fix"; `agent.py:313` promises "/addfood coming". Neither is
registered, and text-logged meals get `quick_actions()` rather than `meal_actions()`,
so a typed meal cannot be corrected at all.

**D7. `verify_init_data` will reject every real initData.** `api.py:96` splits the
query string but never percent-decodes the values before building the data-check
string, which Telegram's scheme requires. The `user` field is always URL-encoded JSON,
so the HMAC can never match. Latent until the Mini App exists, but it is the kind of
thing that costs an afternoon later.

**D8. `/healthz` does not check the database.** It returns `{"ok": true}` unconditionally,
so the compose healthcheck reports "healthy" for a container that cannot reach Postgres.

**D9. The allowlist is attached per-observer, not to the update.**
`app.py:67` registers the middleware on `message` and `callback_query`. Any future
handler for `edited_message`, `inline_query` or `my_chat_member` would be unguarded.
`dp.update.middleware` is the one place that cannot be forgotten.

**D10. A latent hole in the safety floor.** `safety.decide_target` clamps `true_target`
to `floor_kcal`, then multiplies by `clamp_k(k)`, which can be as low as 0.70. The
number actually shown can therefore sit 30% below the absolute floor the module exists
to enforce. Defensible — the shown target is in *reported* units, not eaten ones — but
it is undocumented and unclamped, and Phase 3 is when it starts to matter. Phase 1
always passes `k=1.0`, so it is latent today.

### E. Testing and operations

**E1. The integration tests are not isolated from the data in the database.** The
session fixture rolls its own writes back, but assertions read table-wide:
`test_a_gram_correction_supersedes_rather_than_mutates` does `select(Correction)` and
asserts one row. Run against the dev database, which now holds the two corrections
from the live session, it fails with `assert 3 == 1`. The suite is green only against
a virgin database — so the "115 tests green" claim above is conditional. Scope those
assertions to the entry under test.

**E2. Analytics is unreachable from the application.** `analytics/trend.py`,
`calibration.py` and `correlations.py` are imported only by `tools/simulate.py`.
`trend_weight`, `daily_rollups`, `calibration_state`, `insights` and `commitments` are
never written. This is the deliberate Phase 1 deferral and not a defect — but the
weight-trend series is cheap and worth starting now, because Phase 3 will want
history that only exists if something has been recording it.

**E3. Production compose sets no `TZ` and no app healthcheck.**
`docker-compose.yml` gives the app neither, while `docker-compose.app.yml` has a
healthcheck. With A5, `TZ` is load-bearing.

**E4. Stale scratch files in the tree.** `tests/__pycache__` holds
`scratch_test1..6.cpython-313` bytecode for test modules that no longer exist.

### Suggested order

A1, A2, A5, A6 are one-line fixes with real consequences. A3/A4 are the difference
between a bot that stalls for half a minute and one that does not. B3 then B1/B2 are
where accuracy actually lives: until names are short the resolver cannot match, and
until the provisional path is wired an unmatched item silently reads as zero calories.
C1–C4 are the difference between a correction flow that works twice and one that works
once. Everything in D and E can wait for a quiet afternoon.


---

## Audit fixes and the enrichment job (2026-08-22, later)

Two pieces of work: every finding in the audit above, and the background job the
food table needed to stop reporting real meals as ten calories.

### The enrichment job — the food table now fills itself

`core/enrichment.py`. When the resolver cannot match an item, the gap is
remembered; a job researches it off the critical path and backfills the items
that were waiting.

  * **Routing.** A new `enrichment` task on the coach tier (`glm-5.3`, the
    largest model configured), because composition recall is the one task where
    breadth of world knowledge *is* the product. `temperature=0` so the same
    dish asked twice gives the same answer; `effort: "medium"`, the only task
    above low, because the model has to make the Atwater arithmetic hang
    together.
  * **Nothing it says is trusted.** `validate()` gates every candidate: energy
    below pure fat, macros non-negative and summing under 100g in 100g of food,
    and the load-bearing check — 4·protein + 4·carbs + 9·fat must reconstruct
    the stated energy within max(60 kcal, 30%). A model confabulating a
    composition rarely confabulates a self-consistent one.
  * **Tier 4, always.** `trust_tier=4`, `source=model`, `verified_at` null,
    however confident the model sounds. An existing row is never overwritten, so
    a tier-1 lab row survives a tier-4 guess arriving later under the same name.
  * **It has a memory.** `enrichment_attempts` (new table) keys on the same
    name+state the resolver failed on and records the rejection reason. Three
    failures and the name is left alone — each attempt costs a call to the most
    expensive model in the roster.
  * **Scheduling.** Frequency-ordered (what you eat weekly outranks March's side
    salad), four gaps per tick, every 20 minutes, plus a fire-and-forget nudge
    when a photo leaves gaps. A Postgres advisory lock means overlapping runs
    cannot both research the same gap; each gap gets its own transaction, so one
    bad answer does not roll back two good rows.
  * **The backfill is legitimate, not a violation.** Macro columns are a cache,
    always recomputable from `food_id` plus grams. No entry is superseded, no
    gram value changes, and the arithmetic is the same `tools.macros_for_food`
    the original write used — extracted from `_macros_for` so the raw-vs-cooked
    and absorbed-oil rules keep exactly one home.

**Verified live**, against the twelve orphaned items from the first session:

| detected name | became | result |
|---|---|---|
| `flatbread with reddish meat/pepper paste topping (turkish pide-style flatbread)` | **lahmacun**, 256 kcal/100g, tier 4 | 220 g → 563 kcal |
| `brisket` | beef brisket, 207 kcal/100g | 200 g → 414 kcal |
| `french fries` | french fries, 312 kcal/100g | 180 g → 562 kcal |
| three caption-shaped names | rejected, "no canonical name" | left for a human |

The meal that reported **"Total 10 kcal"** now reports **573 kcal**. Aliases come
back in the local language (`patates kızartması`, `kıymalı pide`, `döş eti`), so
the same dish typed in Turkish resolves on the *next* log without a model call at
all. Cost: 4 calls ≈ $0.009 per tick, and only when there are gaps.

One live-fire correction: the first run rejected all four gaps because glm-5.3
answers in `json_object` mode and simply omitted `is_food`, which the validator
read as a denial. Now only an explicit `false` counts — the arithmetic gates are
the real guard.

### Cuisines — the accuracy lever, and the reason lahmacun resolves

`core/cuisines.py`, `users.cuisines`, `/cuisines`. A curated list of 19
traditions, max 6, toggled from an inline keyboard, seeded from `UMAI_CUISINES`.
The same list reaches three places: the perception prompt, the text classifier,
and the enrichment job.

This is not a preference setting, which is why it is not free text — a typo
would quietly degrade every photo. It is the cheapest accuracy available
anywhere in the pipeline: a model reading a plate cold writes "flatbread with
reddish meat/pepper paste topping", which matches nothing and logs as zero; told
this person eats Turkish food, it writes "lahmacun", which is a lookup key. The
normalisation is order-stable on purpose, because an unstable prompt is an
unstable fingerprint and `perception_runs` exists to tell prompt drift from
model drift.

### What each audit finding became

**A1** `init_engine` no longer has its factory captured by value — `get_factory()`
resolves at call time, and the evening summary is handed a real factory.
**A2** `ModelClient.bind_loop()` at the composition root; `_dispatch_usage` uses
`run_coroutine_threadsafe`, so the callback survives being fired from a worker
thread. **A3** `acall()` runs the sync SDK on a thread and is now the only form
async callers use — perception, routing, the resolver tiebreak and enrichment all
went through it, and `PerceptionClient.analyse` is async (image preparation on a
thread too). **A4** per-task timeouts, 20 s for routing up to 120 s for
enrichment, `max_retries=1` under the fallback chain. **A5** the cron trigger
carries `ZoneInfo(settings.tz)`; `TZ` added to both compose files. Verified: the
trigger reports `Europe/Istanbul`. **A6** default parse mode is now `None`, so
"fish & chips" can no longer make Telegram reject an entire reply.

**B1** an incomplete total is never printed as a total — items are marked
"⏳ looking it up" and the line reads "At least 573 kcal (2 items still being
looked up)", or, if nothing matched, no number at all. **B2/B3** covered above;
the perception schema now separates a short `name` (a lookup key, ≤4 words,
parentheticals and "X with Y" tails stripped in a validator) from a free-text
`description` the resolver never sees. **B4** media rows are deduplicated on a
now-unique `sha256` and linked to their entry. **B5** `perception_runs` is
written with the raw response, model, latency and prompt fingerprint. **B6/B7**
`tools.remember()` writes the library (incrementing `times_logged`) and
recomputes portion priors in SQL after every meal, and the priors reach the
perception prompt — resolution tier 2 and the "shrinks with use" mechanism are
both live rather than inert. **B8** absorbed frying oil is applied when the item
was fried and the row is not already the fried food.

**C1** a correction re-sends the meal with a keyboard pointing at the new entry,
so a second fix works. **C2** "Fixed." is only said when something was.
**C3** only the corrected item is stamped `grams_source=user`. **C4** zero grams
removes the item instead of leaving a phantom. **C5** a photo clears a pending
number prompt. **C6** the prefix lookup is scoped to the user and to food entries.

**D1** typed food is `user` or `vlm` by whether the classifier guessed the
amount. **D2** a zero ml/kg asks again instead of falling through to "I didn't
catch that". **D4** no transaction is held across the vision call — the photo
handler reads context, closes, calls the model, then opens a write transaction.
**D5** "Looking…" is deleted in a `finally`. **D6** `/fix` and `/addfood` are
gone from the copy; typed meals get the real correction keyboard. **D7** initData
is percent-decoded before the HMAC. **D8** `/healthz` runs `SELECT 1` and returns
503 if the database is unreachable. **D9** the allowlist moved to
`dp.update.outer_middleware`, where it cannot be forgotten by a future handler.

**D10 is deliberately unchanged.** The shown target can sit below the absolute
floor when `k < 1`, because it is expressed in *reported* units rather than eaten
ones — clamping it would break the calibration arithmetic it exists to serve.
Phase 1 always passes `k=1.0`, so it is latent. Revisit when Phase 3 turns
calibration on, and document the distinction wherever the number is displayed.

**E1** the correction test scopes its assertion to its own entry, so the suite
passes against a database that has been used. **E3** `TZ` and an app healthcheck
in the production compose file. **E4** stale `scratch_test*` bytecode removed.

### State

139 tests green (was 115), ruff and mypy clean, wall-clock ban holds. New
migration `a3f1c7e94b20` (user cuisines, enrichment_attempts, unique media
sha256) applied to the dev database. The container is rebuilt and polling, both
jobs scheduled, `/healthz` reporting `{"ok":true,"db":true}`.

### Worth knowing next

  * Three of the first session's gaps are unresearchable because their *names*
    are captions from the old schema. New photos cannot produce those names, so
    this is a one-off; the rows can be left or deleted.
  * The enrichment model is asked one dish at a time. If the food table stays
    sparse for weeks this is worth batching, but at four gaps per 20 minutes it
    is nowhere near a cost concern yet.
  * `enrichment_attempts.last_error` is the thing to read after a week of real
    use — it says exactly which dishes the largest model cannot describe
    consistently, which is the list worth hand-transcribing.


---

## Status against the plan (2026-08-22, development paused here)

Written at the point of stopping for a real testing round. The question this
answers is "what can I actually use tomorrow, and what will I notice is
missing".

### Phase state

| Phase | Plan says | Actually |
|---|---|---|
| **-1** Baseline eval | Twenty photos, three repeats, bias *and* variance known | **Half done.** Variance measured and passed (mean CV 0.07, worst 0.11, gate 0.20). Bias never measured: `eval/truth.csv` holds 3 rows covering 2 photos, so MAPE against ground truth was never computed. The harness works; the ground truth was never written. |
| **0** Foundations | Pi, Compose, Postgres, migrations, bot echoing, health ingest landing, backups | **Done except the Pi.** Everything runs in Docker on the Mac. Health ingest is written, idempotent and tested, but has never received a payload from the real Health Auto Export app. No Pi, no Tailscale, no backups running. |
| **0.5** Seed the food table | USDA + 150-300 TurKomp rows + label-photo importer | **Partly, and now partly obsolete.** 46 hand-transcribed Foundation rows; USDA live import needs a free key; TurKomp CSV is empty. The label-photo importer is written but unreachable from chat. The enrichment job now covers much of what this phase existed to do. |
| **1** The logging loop | Four-stage pipeline, text *and voice*, one-tap buttons, manual weight, daily summary, static targets | **Done except voice.** Everything else works end to end. This is the phase whose gate is three weeks of real use, which is what the pause is for. |
| **2** Recipes and learning | Recipe creation and recognition, library recall with embeddings, portion priors | **Roughly a third.** Portion priors and the personal library now accumulate and are used. Recipes exist in the schema and the resolver reads them, but nothing can create one. No embeddings. |
| **3** Calibration and coaching | Trend EWMA, calibration engine, adaptive targets, proactive coaching, check-ins | **Written, not connected.** `analytics/trend.py` and `calibration.py` are complete and simulator-tested, and reachable only from `tools/simulate.py`. Deliberate: they need weeks of history first. No coaching, no check-ins. |
| **4** Depth | Mini App, correlations, weekly review, satiety, weekly banking, restaurant mode | **Almost nothing.** `analytics/correlations.py` is complete and imported by *nothing at all*. The rest is unbuilt. |
| **5-6** | Anticipation, multi-user | Not started, correctly. |

### What works right now

Photo logging end to end: perception → resolution → arithmetic → one
confirmation, with per-item gram correction that now survives being used twice.
Text logging in English and Turkish. Bare numbers read as weigh-ins. Water and
weigh-in buttons. `/summary`, `/week`, `/cuisines`. The evening summary at 21:30
in the right timezone. Health ingest (untested against the real app). The
enrichment job filling the food table by itself, cuisine-aware. The personal
library and portion priors accumulating. API spend recorded. Safety floors on
every target.

### What is missing, in the order it will be missed

1. **Voice notes.** The plan calls this "the lowest-friction capture method that
   exists" and the handlers module docstring claims to implement it. Nothing
   does. `F.voice` has no handler; a voice note is silently ignored. Batch
   backfill ("one voice note describing a whole day") depends on it.
2. **Recipes.** Schema, resolver tier and compute path all exist; there is no
   way to create one. Plan calls this the most accurate path in the system and
   the largest friction win for anyone who cooks. Currently dead weight.
3. **Meal-from-library one-tap.** The library now fills, so "the usual" is
   possible for the first time — but nothing surfaces it.
4. **Dinnerware calibration.** Read into every perception prompt, written by
   nothing. This is the plan's highest-value-per-effort portion anchor and the
   one-off onboarding flow for it does not exist.
5. **The ground truth for Phase -1.** Twenty photos with weighed items. Without
   it, bias is unknown — which is tolerable, since the calibration engine exists
   to absorb bias, but it means the eval harness cannot rank a model change.
6. **Label-photo and barcode import.** Both written, neither reachable from
   chat. A photographed label is the cheapest tier-2 row available.
7. **Satiety, alcohol, fasting window, body measurements.** Cheap to collect,
   and satiety in particular unlocks the analysis the plan calls the most
   personally useful in the system.
8. **Check-ins, coaching, weekly review, correlations, explain-why, Mini App,
   export, cost reporting.** Phase 3/4 proper. Correctly deferred until there is
   history worth analysing.

### Known holes to keep in mind while testing

  * A photo still takes 15-25 seconds. It no longer blocks anything else, but it
    is the slowest interaction in the product.
  * The enrichment job runs every 20 minutes; totals for a novel food fill in
    after the fact rather than immediately. The reply says so ("looking it up").
  * `enrichment_attempts.last_error` is worth reading after a week — it names
    the dishes the largest model cannot describe consistently, and that list is
    the one worth hand-transcribing into tier 1.
  * The three unresearchable gaps from the first session are artefacts of the
    old caption-shaped names, not a live bug.
  * `tools/benchmark_perception.py` and `tools/replay_health.py` are
    docstring-only stubs.
  * Health ingest has never seen a real payload.

### Suggested order when development resumes

Voice, then recipes and meal-from-library, then dinnerware onboarding — all
Phase 1/2 friction, which is what the three-week gate actually measures. Phase 3
analytics stays parked until the history exists to fit it against.


---

## pgAdmin and the sql/ queries (2026-08-22, after the pause)

`docker-compose.dev.yml` now brings up pgAdmin alongside Postgres on
**http://localhost:5050**. `just db` starts both; `just pgadmin` opens it.

No login, no master password, no connection dialogue: it runs in desktop mode
with the server pre-registered from `ops/pgadmin/servers.json` and the password
read from a mounted `ops/pgadmin/pgpass`. Those three rituals would otherwise be
paid again every time the volume is recreated. Bound to `127.0.0.1` only — the
container holds database credentials and answers anyone who can reach the port.

Deliberately **not** in `docker-compose.yml` (the Pi): real health data on a
tailnet-reachable machine, and a second credentialed web UI buys nothing there
that `just psql` over ssh does not.

### The queries

Eleven ready-made queries in `sql/`, mounted into the container at `/sql` so they
open from the Query Tool (**File → Open**), and runnable from the terminal with
`just q sql/today.sql`. `just q-check` runs all of them and fails loudly, so a
schema change that breaks one is caught then rather than in the browser weeks
later. All eleven verified against the live schema.

They exist because the interesting reads are joins: what you ate is
`log_entries` → `food_items` → `foods` filtered on `superseded_by IS NULL`, and
omitting that filter silently double-counts every corrected meal. `unmatched.sql`
and `enrichment.sql` are the two to watch while testing — the first is what the
researcher is chasing, the second is what it added and what it refused.

### Two pgAdmin gotchas, recorded so they are not rediscovered

It **validates `PGADMIN_DEFAULT_EMAIL` and then restarts in a loop rather than
reporting the problem**. `umai@localhost` is rejected (no dot in the domain) and
`dev@umai.local` is rejected (`.local` is a reserved name); `dev@umai.dev`
works. Nothing is ever sent to it. Also: the `pgpass` must be mode 600, so it is
committed with those permissions rather than generated. Inside the container the
database is `db:5432`, not `localhost:5433`.

### Incidentally confirmed working in production

Starting pgAdmin surfaced live evidence that two of the audit fixes hold. From a
photo sent after the rebuild: `perception_runs` has a row with both `entry_id`
and `media_id` linked (B4, B5 — previously nothing was ever written, and both
media rows had a null entry), and `api_usage` holds 10 rows across perception,
routing and enrichment (A2 — previously empty through a whole session of paid
calls). Total spend to date $0.022.

**Voice notes stay parked** at your call. They remain the largest Phase 1 gap;
`telegram/handlers/__init__.py` still has a module docstring claiming to handle
them, which is worth correcting when the work is picked up.
