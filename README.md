# Umai — Personal AI Nutrition & Health Assistant

**A self-hosted, always-on personal dietitian running on a Raspberry Pi, accessible entirely
through Telegram.**

Umai is a personal nutrition and health assistant that lives on your own hardware. You send it
a photo of a meal; it identifies the items, estimates the grams, resolves each against a food
composition table, and logs the macros — computed in code, never guessed by a model. Over time
it calibrates its own estimation bias against your weight trend, so the numbers it shows you
stay honest even when the estimates are systematically off.

*The name comes from Umay, the Turkic goddess associated with protection, nurturing, and
wellbeing.*

---

## Quickstart (dev, macOS)

```bash
brew install uv just docker   # prerequisites
just db                       # Postgres 17 + pgvector in Docker, port 5433
just migrate                  # schema
just seed                     # 46 starter foods (no API key needed)
just dev                      # the bot: long polling + health ingest endpoint
```

Then message your bot on Telegram. Photos log meals; text like "200g rice and chicken" or
"iki yumurta" logs too; bare numbers between 30 and 250 log as weigh-ins.

Secrets and personal config live in `.local.env` (gitignored):

```bash
TELEGRAM_BOT_TOKEN=           # from @BotFather
TELEGRAM_ALLOWED_USER_IDS=    # your numeric id, from @userinfobot. single-user lockout.
OPENROUTER_API_KEY=           # from openrouter.ai
UMAI_SEX=male                 # seeds BMR and the safety floors; targets refuse to
UMAI_HEIGHT_CM=178            #   compute without them.
UMAI_BIRTH_DATE=2000-10-04
UMAI_START_WEIGHT_KG=89.9     # starts the weight series at onboarding
```

See `.env.example` for the full list including the prod-only values.

## How logging works

The four-stage pipeline (plan §4): a vision model reports *items, state, grams, confidence*
only — never calories. Each item resolves against the `foods` table (trigram + a cheap LLM
tiebreak). Macros are multiplication. One confirmation message with per-item fix buttons.

This split is the core design decision: estimates become deterministic, auditable and
correctable, and the system's largest error (portion size) sits in one number anyone can fix
with a tap.

## The rest of the documentation

| Doc | What it holds |
|---|---|
| [`umai-project-plan.md`](umai-project-plan.md) | Product design: vision, calibration engine, pipeline, data model, roadmap. |
| [`technical-implementation.md`](technical-implementation.md) | Stack, dev loop, testing strategy, Mac→Pi deployment. |
| [`progress.md`](progress.md) | One-page done / in-progress / todo. |
| [`eval/README.md`](eval/README.md) | The photo-estimation baseline harness and its decision gates. |

## Deploying to the Pi

Both Mac and Pi are arm64; one image runs on both. `just build` → `just deploy` (build on the
Mac, never on the Pi). Dev runs long polling; the Pi runs the same image with `UMAI_ENV=prod`
(webhook + Tailscale for the Health Auto Export ingest endpoint). Nothing Pi-specific may
enter the code — the only divergences are env vars.

Back up before the first real week of data: `just backup` (nightly `pg_dump` + the photo
directory, off the device).

## Your data stays yours

Self-hosted, local database, one-command export. Model calls are routed with
`provider.data_collection: "deny"`. Telegram transit is the accepted trade for a zero-install
interface — keep clinical detail out of chat.
