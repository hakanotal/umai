# Umai task recipes.  `brew install just` if you have not already.
# `just` on its own lists everything.

set dotenv-load := true
# Keys live in .local.env (gitignored), not .env. Settings also reads both via
# pydantic-settings, so app code is unaffected; this is for recipes that invoke
# scripts reading os.environ directly (preflight, eval, tools/perceive.py).
set dotenv-path := ".local.env"

PI := env_var_or_default("UMAI_PI_HOST", "pi")
IMAGE := env_var_or_default("UMAI_IMAGE", "ghcr.io/OWNER/umai:latest")

default:
    @just --list --unsorted

# --- local development -----------------------------------------------------

# Postgres in Docker, application on the host with hot reload.
db:
    docker compose -f docker-compose.dev.yml up -d
    @echo "postgres on localhost:5433, pgadmin on http://localhost:5050"

db-down:
    docker compose -f docker-compose.dev.yml down

psql:
    psql postgresql://umai:dev@localhost:5433/umai

# Browse the data. No login, the server is already registered; the ready-made
# queries in sql/ are mounted at /sql inside it (Query Tool -> File -> Open).
pgadmin: db
    @echo "http://localhost:5050"
    @open http://localhost:5050 2>/dev/null || true

# Run one of the sql/ queries in the terminal instead: `just q sql/today.sql`
q FILE:
    @docker exec -i umai-db-1 psql -U umai -d umai -f - < {{FILE}}

# Every ready-made query, so a schema change that breaks one is caught here
# rather than in the browser three weeks later.
q-check:
    #!/usr/bin/env bash
    set -euo pipefail
    for f in sql/*.sql; do
        if out=$(docker exec -i umai-db-1 psql -U umai -d umai -q -v ON_ERROR_STOP=1 -f - < "$f" 2>&1 >/dev/null) && [ -z "$out" ]; then
            echo "ok   $f"
        else
            echo "FAIL $f"; echo "$out" | head -3; exit 1
        fi
    done

dev: db
    uv run python -m umai

# The app in a container on the Mac, against the dev DB (verify-before-the-Pi
# from docs/technical-implementation.md section 11). Needs `just db` running.
docker-app: db
    docker compose -f docker-compose.app.yml up -d --build

docker-app-logs:
    docker compose -f docker-compose.app.yml logs -f --tail=50 app

docker-app-down:
    docker compose -f docker-compose.app.yml down

# Migrations against the containerised app's DB, as an explicit one-shot.
docker-migrate:
    docker compose -f docker-compose.app.yml run --rm app alembic upgrade head

# --- schema ----------------------------------------------------------------

migrate:
    uv run alembic upgrade head

revision name:
    uv run alembic revision --autogenerate -m "{{name}}"

# Drop and rebuild the dev database. Dev only, and it means it.
reset-db: db-down
    docker volume rm umai_umai_dev_data || true
    just db
    sleep 3
    just migrate

# --- quality ---------------------------------------------------------------

# Integration tests point at the dev container when it's up (testcontainers
# otherwise, skip if no Docker). `just check` runs the full suite.
test: db
    UMAI_TEST_DATABASE_URL=postgresql+asyncpg://umai:dev@localhost:5433/umai \
        uv run pytest

# One test: just t tests/unit/test_compute.py::test_yield_factor
t target:
    uv run pytest {{target}} -v

# Refresh the recorded model responses. Costs money, makes real calls.
record target:
    RECORD=1 uv run pytest {{target}}

lint:
    uv run ruff check .
    uv run ruff format --check .

fmt:
    uv run ruff check --fix .
    uv run ruff format .

types:
    uv run mypy src/umai

check: lint types test

# The wall-clock ban from docs/technical-implementation.md section 8.
check-clock:
    @! grep -rn "datetime.now\|utcnow" src/umai --include="*.py" \
        | grep -v "src/umai/clock.py" \
        || (echo "wall clock used outside clock.py" && exit 1)

# --- health ingest ---------------------------------------------------------

# Capture one real Health Auto Export payload (port 8010) into tests/fixtures/.
record-health:
    uv run python tools/replay_health.py --record

# Feed a saved payload through the real ingest path. Run twice: the second run
# must write nothing.
replay-health FILE:
    uv run python tools/replay_health.py --replay {{FILE}}

# --- evaluation ------------------------------------------------------------

# Model preflight: live pricing, and a warning if a model lost a capability.
preflight:
    uv run python src/umai/config/models.py

# The twenty-photo baseline. Rank on ratio SD, not MAPE.
eval model:
    cd eval && uv run --with openai --with pillow \
        python run_eval.py --model {{model}} --repeats 3 --out results

# The 24-photo perception run through the real code path (real API, ~$0.08).
perceive dir="data/media" repeats="1":
    uv run python tools/perceive.py {{dir}} --repeats {{repeats}} \
        --out data/perception-results --parallel 6

# --- food data ---------------------------------------------------------------

# Starter foods (46 hand-transcribed USDA Foundation rows) into the dev DB.
# Zero API calls, always safe to run.
seed:
    uv run python tools/seed_foods.py --source starter

# Reduce the FNDDS survey-food CSV bundle in data/ to data/fndds_seed.csv
# (one wide row per food) plus data/fndds_portions.csv. Offline, no DB write:
# the output is meant to be read before it is seeded.
extract-fndds:
    uv run python tools/extract_fndds.py

# Full USDA import. Needs USDA_API_KEY in .local.env (free key:
# https://fdc.nal.usda.gov/api-key-signup.html). DEMO_KEY is throttled to uselessness.
seed-usda:
    uv run python tools/seed_foods.py --source usda

# --- simulation ------------------------------------------------------------

# Plant a known TDEE and a known logging bias, then check the engine recovers them.
sim days="90" tdee="2400" bias="0.78":
    uv run python tools/simulate.py generate --days {{days}} \
        --true-tdee {{tdee}} --logging-bias {{bias}} --seed 42
    uv run python tools/simulate.py run --history sim_{{days}}d.json

# --- deploy ----------------------------------------------------------------

build:
    docker buildx build --platform linux/arm64 -t {{IMAGE}} --push .

# Verify the production image on the Mac before it ever reaches the Pi.
smoke:
    docker compose up --build

deploy: build
    ssh {{PI}} 'cd umai && docker compose pull && \
        docker compose run --rm app alembic upgrade head && \
        docker compose up -d'

logs:
    ssh {{PI}} 'cd umai && docker compose logs -f --tail=100'

backup:
    ssh {{PI}} 'cd umai && docker compose exec -T db pg_dump -U umai umai | gzip' \
        > backups/umai-$(date +%Y%m%d).sql.gz
