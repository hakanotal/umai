# UMAI: Personal AI Nutrition & Health Assistant

**A self-hosted, always-on personal dietitian, accessible entirely through Telegram.**

Umai is a personal nutrition and health assistant that lives on your own hardware. You send it
a photo of a meal; it identifies the items, estimates the grams, resolves each against a food
composition table, and logs the macros — computed in code, never guessed by a model. Over time
it calibrates its own estimation bias against your weight trend, so the numbers it shows you
stay honest even when the estimates are systematically off.

> The name comes from Umay, the Turkic goddess associated with protection, nurturing, and
wellbeing.

## Getting it running

Umai is one process — the Telegram bot, the scheduled jobs and the HTTP ingest endpoint — in
front of a Postgres database. `just` alone lists every recipe; `brew install just` if it is
missing.

```bash
cp .env.example .env       # then fill it in; the file explains each value
just db                    # Postgres 17 + pgvector + pgAdmin, on port 5433
just migrate               # alembic upgrade head
just seed                  # the 46-row USDA starter food table
just dev                   # long polling, hot reload
```

Four values in `.env` decide whether it starts at all: `TELEGRAM_BOT_TOKEN`,
`OPENROUTER_API_KEY`, `UMAI_INVITE_CODE` and `UMAI_BOOTSTRAP_ADMIN_TELEGRAM_ID`. The last is
your own numeric Telegram id — message [@userinfobot](https://t.me/userinfobot) to find it. It
is the one account that skips the invite phrase, because a fresh deployment otherwise has
nobody who can admit anybody, including themselves.

## Letting other people in

Send the bot `/start` from your own account and it takes you straight into onboarding: seven
questions — which city you live in, sex, height, date of birth, current weight, what you are
aiming for, and which cuisines you eat. The city becomes your timezone, which every daily total
is computed from; the bot confirms it by telling you the local time and asking whether that is
right, because a wrong timezone is invisible until a day lands on the wrong date weeks later.

Everybody else needs the phrase in `UMAI_INVITE_CODE`. They message the bot, it asks for the
phrase, and anything else gets "That isn't it" and nothing more — no hint about the length, the
format, or whether they are close. Five wrong guesses and that account is blocked permanently,
which is what makes a short phrase safe; `/unblock <id>` undoes it. A correct phrase drops them
into the same wizard.

Their data is theirs. Separate timezone, separate targets, separate food library and portion
history, separate photos on disk, separate daily digest at whatever hour suits them, and a
separate token for Health Auto Export (`/token`). The food composition table is the one shared
thing, deliberately: a dish researched for one person is correct for everyone, and fixing a row
corrects every meal that ever referenced it.

`/users` lists who is in and who is waiting. `/block <id>` revokes access and keeps the history.
`/export` hands somebody everything held on them as JSON and CSV; `/delete_me` destroys it.
Those last two are not decoration — the moment there is a second person, this is somebody else's
health data.

## Deploying

Railway runs the same image with `UMAI_ENV=prod`, which switches Telegram from long polling to
a webhook. Nothing in the code is platform-specific; the only divergences are environment
variables, which live as Railway variables rather than in a file. Pushing to `main` is the
deploy: the service builds the Dockerfile, runs `alembic upgrade head` as a pre-deploy step,
and only then replaces the running container.

```bash
git push          # the ordinary path
just deploy       # build from the working tree, for a hotfix you have not pushed
just logs
```

The health ingest endpoint is a real HTTPS URL now rather than something reached over a
tailnet, which is the first time a phone has been able to post to it. It is protected by a
per-user bearer token and nothing else — there is no rate limiting on it yet.

## The documentation

| File | What it settles |
|---|---|
| `docs/umai-project-plan.md` | Product design: calibration, the estimation pipeline, the data model, the roadmap. |
| `docs/technical-implementation.md` | Stack, repo layout, dev loop, testing, the Railway deploy, and the gotcha list. |
| `docs/progress.md` | One page: done, in progress, todo. |
| `CLAUDE.md` | The architectural invariants, and what changing one costs. |
| `sql/README.md` | Ready-made inspection queries, mounted into pgAdmin at `/sql`. |
