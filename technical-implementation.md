# Umai: Technical Implementation Plan

*How to build this on your MacBook, verify it works, and only then put it on the Pi.*

**Version:** 1.0
**Date:** 22 August 2026
**Companion to:** `umai-project-plan.md`

---

## 1. Short answer

Yes, develop locally, and not as a convenience. The Pi is a deployment target, not a development
environment. Building on it directly means slow rebuilds, a compiler toolchain on your production
box, and a debugging loop that runs over SSH.

Your Mac and the Pi are both **arm64**, so container images built on one run unmodified on the
other. That is the single fact that makes this easy. There is no cross-compilation problem, no
architecture surprises at deploy time, and no "works on my machine" that traces back to CPU
instruction sets.

What makes this app awkward to develop locally is not the code. It is four external dependencies
that assume a publicly reachable server. Each has a clean answer.

| Obstacle | Why it hurts | Answer |
|---|---|---|
| Telegram normally pushes via webhook to a public URL | Your laptop has no public URL | Use long polling in dev, webhook only in prod. It is a config flag, not a code path. |
| Health Auto Export POSTs from your phone to a server | Your phone cannot reach `localhost` | Tailscale gives your Mac a stable address your phone can reach from anywhere. Plus recorded fixtures for repeatable tests. |
| Every model call costs money and returns something different each time | Tests become expensive and flaky | Record real responses once, replay them forever. Tests cost nothing and are deterministic. |
| The core features are time-dependent | You cannot wait 14 days to test the calibration engine | A clock abstraction plus a synthetic history generator plus a simulator. Covered in section 8, and it is the most important part of this document. |

---

## 2. Prerequisites on the Mac

```bash
brew install --cask docker          # Docker Desktop. Docker runtime for local development.
brew install uv                     # Python packaging. Replaces pip, venv, poetry, pyenv.
brew install just                   # Task runner. Optional but the recipes below assume it.
brew install tailscale              # Reaching your Mac from your phone.
brew install postgresql@17          # For psql only. The server runs in Docker.
```

Docker Desktop runs arm64 containers natively on Apple Silicon, no emulation involved, so the
same portability argument in section 11 (build once, run on Mac and Pi) holds regardless.

`uv` is worth adopting even if you have never used it. It resolves and installs dependencies
in a fraction of the time of pip or poetry, manages the Python version itself, and produces a
lockfile. On a Pi, where every install is slow, that difference is felt rather than measured.

---

## 3. Stack

| Layer | Choice | Why, and what else was considered |
|---|---|---|
| Language | Python 3.13 | The ecosystem for this problem (image handling, numerics, model SDKs) is Python. Nothing else is close. |
| Packaging | `uv` | Fast, lockfile-based, manages the interpreter. Poetry works but is slower everywhere and much slower on the Pi. |
| Telegram | `aiogram` 3.x | Async-first, clean FSM for multi-step flows like recipe creation, and switching between polling and webhook is one line. `python-telegram-bot` is equally mature; pick either and do not revisit. |
| Web | FastAPI | Serves the health ingest endpoint and the Mini App backend in one process. Async, and Pydantic models double as the model output schemas. |
| ORM | SQLAlchemy 2.0 (async) | Typed, mature. Alembic for migrations is the real reason. |
| Migrations | Alembic | Non-negotiable once real data exists on the Pi. |
| Database | Postgres 17 + pgvector 0.8.x | Official `pgvector/pgvector:pg17` image. See section 9 on whether you need vectors at all in Phase 1. |
| Scheduler | APScheduler with a Postgres jobstore | Survives restarts, which cron in a container does not. Jobs are code, so they are testable. |
| Model calls | `openai` SDK against OpenRouter | OpenRouter is OpenAI-compatible. One SDK, `base_url` swapped. Already written in `config/models.py`. |
| Validation | Pydantic v2 | The stage 1 schema is a Pydantic model, and `model_json_schema()` feeds it straight to the API. One definition, no drift. |
| Charts | Matplotlib, Agg backend | Headless PNG rendering into Telegram. Boring and reliable. |
| Images | Pillow | Resize before sending. See the gotcha in section 12. |
| Logging | structlog | JSON in prod, readable in dev. |
| Tests | pytest, pytest-asyncio, testcontainers | Real Postgres in tests, not SQLite. Vector and trigram behaviour do not exist in SQLite. |
| Lint and format | ruff | Replaces black, isort, flake8. One tool, one config. |
| Types | mypy, strict on `analytics/` and `resolver/` | Full strict everywhere is not worth it. Those two modules are where a silent type error becomes a wrong calorie target. |

---

## 4. Repository layout

```
umai/
├── pyproject.toml                 # uv project, deps, ruff and mypy config
├── uv.lock
├── justfile                       # dev, test, migrate, deploy recipes
├── .env.example                   # committed. .env is not.
├── docker-compose.dev.yml         # local: Postgres only
├── docker-compose.yml             # Pi: full stack
├── Dockerfile
├── alembic/
│   └── versions/
├── src/umai/
│   ├── config/
│   │   ├── models.py              # already written
│   │   └── settings.py            # Pydantic Settings, env-driven
│   ├── clock.py                   # the time abstraction. read section 8.
│   ├── db/
│   │   ├── models.py              # SQLAlchemy tables from plan section 6.3
│   │   └── session.py
│   ├── perception/
│   │   ├── schema.py              # Pydantic stage 1 schema
│   │   ├── prompt.py              # dinnerware, priors, library candidates
│   │   └── client.py              # the vision call
│   ├── resolver/
│   │   ├── match.py               # recipe, library, foods lookup
│   │   ├── compute.py             # grams x per-100g. pure functions.
│   │   └── importers/             # turkomp, usda, openfoodfacts, label_photo
│   ├── core/
│   │   ├── agent.py               # intent routing, tool dispatch
│   │   ├── tools.py               # the tool implementations
│   │   └── prompts/               # persona, coaching constraints
│   ├── telegram/
│   │   ├── app.py                 # polling in dev, webhook in prod
│   │   ├── handlers/
│   │   └── keyboards.py
│   ├── ingest/
│   │   └── health.py              # Health Auto Export webhook
│   ├── analytics/
│   │   ├── trend.py               # EWMA
│   │   ├── calibration.py         # the k factor fit
│   │   ├── correlations.py        # statistics, with thresholds
│   │   └── charts.py
│   ├── scheduler/
│   │   └── jobs.py
│   └── web/
│       ├── api.py                 # Mini App backend
│       └── static/
├── tests/
│   ├── cassettes/                 # recorded model responses
│   ├── fixtures/                  # real Health Auto Export payloads
│   ├── unit/
│   ├── integration/
│   └── sim/                       # synthetic history, calibration simulator
└── tools/
    ├── seed_foods.py
    ├── replay_health.py
    ├── simulate.py
    └── benchmark_perception.py    # wraps eval/run_eval.py against the real code path
```

---

## 5. The development loop

```mermaid
flowchart LR
    subgraph mac["MacBook"]
        direction TB
        code["Source<br/>hot reload via uv"]
        pg[("Postgres 17 + pgvector<br/>Docker, port 5433")]
        code <--> pg
    end

    subgraph external["External"]
        direction TB
        devbot["Telegram<br/>umai_dev_bot"]
        or["OpenRouter"]
    end

    phone["iPhone<br/>Health Auto Export"]

    code -- "long polling" --> devbot
    devbot -. "your messages" .-> code
    code -- "live calls in dev<br/>cassettes in tests" --> or
    phone -- "POST over Tailscale" --> code

    style mac fill:#1f2937,stroke:#4b5563,color:#e5e7eb
    style external fill:#312e2b,stroke:#57534e,color:#e7e5e4
```

Postgres runs in Docker; the application runs on the host with hot reload. Containerising the app
during development costs you a rebuild on every edit and buys nothing, since the Dockerfile is
verified separately before each deploy.

Port 5433, not 5432, so a local Postgres install cannot silently shadow the container.

pgAdmin rides along in the same file, on `localhost:5050`, because almost nothing interesting in
this schema lives in one table: what you ate is `log_entries` joined to `food_items` joined to
`foods`, filtered on the supersede chain, and getting that filter wrong silently double-counts
every corrected meal. It runs in desktop mode with the server pre-registered and the password in
a mounted `pgpass`, so there is no login, no master password and no connection dialogue — three
small rituals that would otherwise be paid again every time the volume is recreated. The
ready-made queries in `sql/` are mounted at `/sql` inside it and open from the Query Tool.

It is deliberately absent from the Pi's `docker-compose.yml`. That machine holds real health data
and is reachable over a tailnet; a second web UI carrying database credentials is attack surface
bought for nothing, when `just psql` over ssh answers the same questions.

---

## 6. Setup, start to finish

```bash
mkdir umai && cd umai
uv init --python 3.13
uv add aiogram fastapi uvicorn sqlalchemy[asyncio] asyncpg alembic \
       pydantic pydantic-settings openai pillow matplotlib \
       apscheduler structlog httpx
uv add --dev pytest pytest-asyncio testcontainers ruff mypy

docker compose -f docker-compose.dev.yml up -d      # Postgres + pgAdmin
uv run alembic upgrade head                          # schema
uv run python tools/seed_foods.py --source usda --top 200
just dev                                             # bot + api, hot reload
```

`docker-compose.dev.yml`, complete:

```yaml
services:
  db:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_USER: umai
      POSTGRES_PASSWORD: dev
      POSTGRES_DB: umai
    ports: ["5433:5432"]
    volumes: ["umai_dev_data:/var/lib/postgresql/data"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U umai"]
      interval: 5s

  pgadmin:
    image: dpage/pgadmin4:9.17
    depends_on:
      db: {condition: service_healthy}
    environment:
      PGADMIN_DEFAULT_EMAIL: dev@umai.dev
      PGADMIN_DEFAULT_PASSWORD: dev
      PGADMIN_CONFIG_SERVER_MODE: "False"
      PGADMIN_CONFIG_MASTER_PASSWORD_REQUIRED: "False"
      PGADMIN_CONFIG_UPGRADE_CHECK_ENABLED: "False"
    ports: ["127.0.0.1:5050:80"]
    volumes:
      - ./ops/pgadmin/servers.json:/pgadmin4/servers.json:ro
      - ./ops/pgadmin/pgpass:/pgpass:ro
      - ./sql:/sql:ro
      - umai_pgadmin_data:/var/lib/pgadmin
volumes:
  umai_dev_data:
  umai_pgadmin_data:
```

Two gotchas worth writing down, because both cost more time than they should. pgAdmin **validates
`PGADMIN_DEFAULT_EMAIL` and restarts in a loop rather than reporting the problem** if it dislikes
it: `umai@localhost` is rejected for having no dot, and `dev@umai.local` for `.local` being a
reserved name. Nothing is ever sent to the address; it just has to look real. And the `pgpass`
must be mode 600, which is why it is committed with those permissions rather than generated.

Inside the container the database is `db:5432`, not `localhost:5433` — pgAdmin is on the compose
network, so it reaches Postgres directly rather than through the host port mapping.

---

## 7. The three external dependencies

### 7.1 Telegram

**Create a second bot.** `@BotFather` gives you `umai_dev_bot` alongside the real one. This is not
optional: Telegram allows one consumer per token, so a dev instance polling the production token
steals messages from the production bot. Two tokens, two `.env` files, no collisions.

**Long polling in dev, webhook in prod.** aiogram makes this a branch at startup, not two code paths:

```python
if settings.env == "dev":
    await dp.start_polling(bot)
else:
    await bot.set_webhook(settings.webhook_url, secret_token=settings.webhook_secret)
```

Polling needs no public URL, no tunnel and no TLS. Latency is a second or two, which is invisible
for this application.

Test with your real Telegram account against the dev bot. There is no useful simulator, and there
is no substitute for feeling how the interaction actually lands.

### 7.2 Health data

**Tailscale is the answer, in dev and in prod.** Install it on the Mac, the Pi and the phone.
Every device gets a stable address on your private tailnet, with no ports opened and no tunnel
service in the path.

Do not widen the container's port binding to reach it. Both compose files publish
`127.0.0.1:8000:8000` and should stay that way; the tailnet exposure is a proxy on the host:

```bash
tailscale serve --bg --set-path /ingest/health http://127.0.0.1:8000/ingest/health
tailscale serve status      # prints the https://<host>.<tailnet>.ts.net URL
```

The flag spelling has moved between client versions, so `tailscale serve --help` outranks the
line above; MagicDNS and HTTPS Certificates both need enabling in the admin console or `serve`
will not bind TLS. The path scoping is deliberate — a bare proxy would publish `/webhook/telegram`
and `/healthz` to every device on the tailnet as well. Widen it when the Mini App arrives.

The alternative, publishing the container port on the host's `100.x` tailnet address, works but
makes the compose file machine-specific, which is exactly the Mac-versus-Pi divergence the setup
exists to avoid, and it gives plaintext HTTP where `serve` gives a real certificate. Binding
`0.0.0.0` is worse still: the Pi sits on a home LAN, and the endpoint's only other protection is
one static bearer token with no rate limiting.

**Health Auto Export configuration.** One automation, REST API type, POST to
`https://<host>.<tailnet>.ts.net/ingest/health`, with `Authorization: Bearer $HEALTH_INGEST_TOKEN`
as a real header rather than a query parameter. Data type Health Metrics — workout payloads exist
but `extract()` ignores them. **Aggregation hourly, chosen once and never changed:** an hourly
bucket and a daily total both stamp local midnight, so switching later overwrites one hourly row
with the whole day's figure while the other 23 survive, and the day reads roughly double. The
`samples` column in `sql/steps.sql` is the tripwire that makes such a switch visible. The REST
export is a paid feature; check the current price in the App Store rather than trusting a figure
quoted here.

Leave any "split by source" setting off. HealthKit already de-duplicates overlapping samples
across devices when you read an aggregate; a per-source breakdown would put two rows on the same
`(user, metric, recorded_at, source)` key, and the upsert would silently keep only the last.

**Then record what arrives.** The first successful POST is worth capturing permanently:

```bash
uv run python tools/replay_health.py --record   # saves the next payload to fixtures/
uv run python tools/replay_health.py --replay tests/fixtures/health_2026-08-22.json
```

After that, developing the ingest path needs no phone at all. It also gives you the real payload
shape rather than what the docs claim, which is usually a small but expensive difference.

Two properties the ingest endpoint must have from the first commit, because the phone will
eventually deliver three days of backlog in one request after being offline: **idempotent**
(a natural key on metric plus timestamp plus source, upsert not insert) and **backfill-tolerant**
(never assume the payload is about today).

### 7.3 Model calls

**Live in dev, recorded in tests.** Development against the real API is fine, since a day of
active work is a few cents. Tests must never call out, both for cost and because a
non-deterministic dependency makes failures meaningless.

Record once, replay always:

```python
# tests/conftest.py
@pytest.fixture
def cassette(request, monkeypatch):
    path = CASSETTES / f"{request.node.name}.json"
    if os.getenv("RECORD"):
        yield from _record_to(path)  # real call, saved
    else:
        monkeypatch.setattr(ModelClient, "call", _replay_from(path))
        yield
```

```bash
RECORD=1 uv run pytest tests/integration/test_perception.py   # refresh cassettes
uv run pytest                                                  # normal, offline, free
```

Commit the cassettes. They double as a regression suite: when you re-record after a model or
prompt change, the diff shows you exactly how behaviour moved.

---

## 8. Time travel, and why it is the important part

The features that make Umai worth building are all time-dependent. Trend weight needs weeks.
The calibration engine needs a fourteen-day window before it produces anything. Correlations
need months. Evening check-ins depend on it being evening.

You cannot develop these by waiting. Three pieces make them testable in seconds.

### A clock abstraction

Nothing anywhere calls `datetime.now()`. Everything takes a clock.

```python
# src/umai/clock.py
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self):
        return datetime.now(tz=UTC)


class FakeClock:
    def __init__(self, start):
        self._t = start

    def now(self):
        return self._t

    def advance(self, **kw):
        self._t += timedelta(**kw)
```

Add a lint rule banning `datetime.now` outside `clock.py`. It is a two-line grep in CI and it
saves the entire testing strategy from erosion.

### A synthetic history generator

Produce a plausible person: 90 days of meals, weights, steps and sleep, with a **known** true
intake and a **known** logging bias applied on top. You now have ground truth that reality
cannot give you.

```bash
uv run python tools/simulate.py generate \
    --days 90 --true-tdee 2400 --logging-bias 0.78 --seed 42
```

### A simulator

Replay that history through the real analytics code and check that it recovers what you planted.

```bash
uv run python tools/simulate.py run --history sim_90d.json
```

```
day 14   k=1.02  tdee=2310  (seeded, wide interval)
day 28   k=1.19  tdee=2385
day 42   k=1.26  tdee=2402
day 56   k=1.28  tdee=2398   true k=1.282, true tdee=2400
converged in 56 days, final error 0.2%
```

This is the highest-value test in the project. It answers the question the whole design rests on,
which is whether the calibration engine actually converges, and it answers it in about a second
instead of two months. It also lets you tune the learning rate, the window length and the
clamps against something measurable rather than by intuition.

Run it against deliberately hostile histories too: a week of no logging, a holiday, a period of
water retention masking real loss, a genuine plateau. If the engine produces a sane answer for
all of those, it will survive you.

---

## 9. A simplification worth taking

**Phase 1 does not need pgvector.**

With 200 to 300 foods and a handful of recipes, Postgres `pg_trgm` fuzzy matching against
`canonical_name_en` plus the `aliases` array resolves names well, using an index, with no model
download, no embedding service and no extra dependency.

```sql
CREATE EXTENSION pg_trgm;
CREATE INDEX foods_name_trgm ON foods USING gin (canonical_name_en gin_trgm_ops);
SELECT id, canonical_name_en, similarity(canonical_name_en, :q) AS s
FROM foods WHERE canonical_name_en % :q ORDER BY s DESC LIMIT 5;
```

Add pgvector in Phase 2, when the personal library and photo-based recipe recognition arrive and
you actually need image similarity. Keep the `pgvector/pgvector` image from day one so the
extension is available the moment you want it, but do not put an embedding model on the Phase 1
critical path.

**A correction to the plan's schema when you do add it.** The data model in section 6.3 specifies
`vector(768)`. That dimension has to match whatever model you choose, and the natural choice here
is CLIP ViT-B/32, which emits **512**. Its real advantage is that image and text land in the same
space, so "find photos that look like lentil soup" works from a text query, which is exactly what
the resolver wants. Use `vector(512)`, and fix the number before the first migration rather than
after, because changing a vector column's dimension later means a rebuild.

CLIP ViT-B/32 runs comfortably on the Mac via MPS and takes roughly a second per image on a Pi 5
CPU. Embedding happens in a background job after logging, so that latency is invisible.

---

## 10. Testing

The value is concentrated, not spread evenly. A wrong number in `analytics/` becomes a wrong
calorie target, which becomes weeks of a stalled deficit. A wrong number in the Telegram keyboard
layout is a cosmetic annoyance. Test accordingly.

| What | How | Priority |
|---|---|---|
| `resolver/compute.py` macro arithmetic, density, yield, fat absorption | Pure unit tests, exhaustive | **Highest.** Pure functions, trivial to test, catastrophic if wrong. |
| `analytics/calibration.py` | The simulator in section 8 | **Highest.** The feature the product rests on. |
| `analytics/trend.py` EWMA | Unit tests with known series | High |
| Safety rails (floors, max loss rate, minimum protein) | Unit tests asserting they cannot be breached | **High.** These exist precisely for the case where everything else is wrong. |
| Resolver matching | Golden file: 100 detected names to expected `foods` rows | High. Regression protection when you change matching. |
| Correlation thresholds | Property test: random noise must produce zero insights | High. This is the guard against confidently invented patterns. |
| Ingest idempotency and backfill | Integration, real Postgres, replay a fixture twice | High. Duplicate steps corrupt the calibration fit. |
| Perception schema handling | Cassettes, including malformed responses | Medium |
| Telegram handlers | A few smoke tests | Low. Manual use finds more. |

The correlation noise test deserves its own mention. Generate pure random data, run the
correlation engine, and assert it reports nothing. If it finds a pattern in noise it will find
patterns in you, and that is the failure mode that destroys trust in a health assistant faster
than any bug.

---

## 11. Mac to Pi

Both are arm64, so images are portable. Do not build on the Pi: it is slow and it puts a build
toolchain on the machine holding your data.

```mermaid
flowchart LR
    dev["MacBook<br/>arm64"] -->|"docker buildx<br/>--platform linux/arm64"| img["Image"]
    img -->|push| ghcr[("GHCR<br/>private")]
    ghcr -->|pull| pi["Raspberry Pi<br/>arm64"]
    pi --> mig["alembic upgrade head<br/>one-shot container"]
    mig --> up["docker compose up -d"]

    style dev fill:#1f2937,stroke:#4b5563,color:#e5e7eb
    style pi fill:#312e2b,stroke:#57534e,color:#e7e5e4
    style ghcr fill:#1e3a3a,stroke:#0f766e,color:#ccfbf1
```

```bash
just build          # docker buildx build --platform linux/arm64 --push
just deploy         # ssh pi 'cd umai && docker compose pull && \
                    #   docker compose run --rm app alembic upgrade head && \
                    #   docker compose up -d'
```

Two differences between the environments, and only two, both in `.env`:

| | Dev (Mac) | Prod (Pi) |
|---|---|---|
| Telegram | long polling, dev bot token | webhook, prod bot token |
| Database | Docker, port 5433, throwaway | Docker on SSD, backed up nightly |

Everything else, including model IDs, schema and business logic, is identical. Keep it that way.
Every divergence between dev and prod is a bug that only appears after deploy.

**Before the first deploy, run the container locally.** `docker compose up` on the Mac using the
production compose file catches the whole class of "works on the host, fails in the container"
problems (missing system libraries, path assumptions, timezone data) while you can still see the
logs in a terminal.

---

## 12. Gotchas

**Resize images before sending them.** A modern iPhone photo is 4032x3024 and roughly 3MB.
Most of that resolution is discarded by the model's tiling anyway, but you pay tokens and latency
for it. Downscale the long edge to about 1024px and re-encode as JPEG quality 85. Expect a
noticeable cost and latency drop with no measurable accuracy change, but verify that on your own
photos with the eval harness before committing to it.

**Store the original anyway.** Send the resized copy, keep the full-resolution file. Re-analysing
old meals with a better model later is one of the quiet advantages of self-hosting, and you cannot
do it from a thumbnail.

**Timezones.** Store every timestamp as UTC with `timezone=True`. Convert at the boundary only.
"Did I log dinner today" is a question about your local day, and a naive datetime somewhere in
the middle will produce a bug that only manifests near midnight and only sometimes.

**Telegram file downloads are two calls.** `getFile` then fetch. Both can fail independently.
Retry the pair, not just the second.

**APScheduler jobs must be idempotent.** A restart mid-job, or a missed window, will re-run it.
Sending the evening summary twice is not a crash but it is exactly the kind of thing that makes
a bot feel broken.

**`asyncpg` and SQLAlchemy async need explicit session scoping.** One session per request or per
handler, never a module-level global. This bites everyone once.

**Never run migrations automatically on container start.** A crash loop then becomes a migration
loop against your real data. Run them as a separate one-shot step in the deploy recipe, as above.

**Back up before the first real week of data, not after.** `pg_dump` plus the photo directory,
encrypted, off the device, nightly. The most annoying possible outcome of this project is losing
two months of logs to an SD card, which is also the most common way Pi projects end.

---

## 13. Local build order

Build in the order that lets you delete an assumption early, not in the order the architecture
diagram suggests.

**Step 1. The perception call, standalone.** No database, no bot. A script that takes a photo path
and prints items, state and grams. Run the twenty-photo eval through it. This validates the single
riskiest assumption in the project before anything is built on top of it.

**Step 2. Schema and the food table.** Alembic migrations, `pg_trgm`, USDA import, hand-seed the
TurKomp subset. Verify that your twenty most common foods each resolve to a tier 1 or 2 row.

**Step 3. Resolve and compute.** Wire stages 2 and 3. Feed step 1's output through them and get
real macros out. Unit test the arithmetic exhaustively. At this point the estimation pipeline is
complete and testable with no Telegram involved at all.

**Step 4. The bot.** Long polling, photo handler, text handler, buttons. This is when it starts
feeling like a product.

**Step 5. Ingest and scheduling.** Health endpoint over Tailscale, record a fixture, daily summary
job.

**Step 6. The simulator, then calibration.** Build the generator and simulator *before* the
calibration engine, so you can develop the engine against a target instead of against a guess.

Steps 1 through 4 are the Phase 1 gate from the project plan. If using it for three weeks is not
pleasant, stop and fix that before touching calibration.

---

## 14. `.env.example`

```bash
UMAI_ENV=dev

TELEGRAM_BOT_TOKEN=              # umai_dev_bot in dev, umai_bot in prod
TELEGRAM_ALLOWED_USER_IDS=       # your numeric id. single-user lockout.
TELEGRAM_WEBHOOK_URL=            # prod only
TELEGRAM_WEBHOOK_SECRET=         # prod only

OPENROUTER_API_KEY=

DATABASE_URL=postgresql+asyncpg://umai:dev@localhost:5433/umai

HEALTH_INGEST_TOKEN=             # bearer token for Health Auto Export
MEDIA_DIR=./data/media

TZ=Europe/Istanbul               # display only. storage is UTC.
```

`TELEGRAM_ALLOWED_USER_IDS` from the first commit. A Telegram bot is discoverable by anyone who
guesses the username, and this is a health assistant with your data in it. One allowlist check in
middleware, before any handler runs.

Sources:
- pgvector, https://github.com/pgvector/pgvector
- aiogram, https://docs.aiogram.dev/
- uv, https://docs.astral.sh/uv/
