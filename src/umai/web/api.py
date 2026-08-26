"""Mini App backend.

Phase 1 serves two routes: the Health Auto Export ingest endpoint and, in prod,
the Telegram webhook that feeds the dispatcher. The Mini App frontend itself is
Phase 4.

The ingest endpoint's bearer token is per user and is what decides whose health
series a payload is written into. There is no longer a deployment-wide token in
front of it: a shared gate adds nothing behind `tailscale serve` and guarantees
that somebody eventually forgets to rotate it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# Imported at module scope deliberately. It was a function-local import inside
# the per-update path, where building aiogram's pydantic validators on first use
# blocked the event loop for well over a second — measurably, a 0.6s sleep took
# 1.97s — which in production is every webhook after a restart stalling the one
# loop the bot, the scheduler and uvicorn all share.
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from umai.config.settings import Settings
from umai.ingest.health import ingest

log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """On shutdown, give the detached update tasks a moment to finish.

    Moving the handler off the request took it out from under uvicorn's
    graceful shutdown, which waits for in-flight *requests* and knows nothing
    about a task. Without this, a deploy landing mid-photo drops the photo —
    the user sent it, the bot acknowledged it, and nothing was ever logged.

    Bounded, because the wait is against a deploy: a photo can legitimately
    take 90s and Railway will not hold the old container open that long, so the
    choice is a short drain or none.
    """
    yield
    if not _IN_FLIGHT:
        return
    log.info("shutdown: draining %d in-flight update(s)", len(_IN_FLIGHT))
    _, pending = await asyncio.wait(set(_IN_FLIGHT), timeout=DRAIN_TIMEOUT)
    if pending:
        log.warning("shutdown: %d update(s) abandoned mid-flight", len(pending))


def create_app(settings: Settings, dispatcher=None, bot=None) -> FastAPI:
    app = FastAPI(title="umai", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.state.settings = settings
    app.state.dispatcher = dispatcher
    app.state.bot = bot
    # Per app instance, not module-level: the tests build several apps, and a
    # shared registry would have one test's update ids suppress another's.
    app.state.seen_updates = SeenUpdates()

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness *and* the database, because the container healthcheck reads
        this. Returning ok unconditionally reported a container that could not
        reach Postgres as healthy, which is worse than having no check."""
        from sqlalchemy import text

        from umai.db.session import session_scope

        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            log.warning("healthz: database unreachable: %s", exc)
            return JSONResponse({"ok": False, "db": False}, status_code=503)
        return JSONResponse({"ok": True, "db": True})

    @app.post("/ingest/health")
    async def ingest_health(
        payload: dict,
        authorization: str = Header(default=""),
    ) -> JSONResponse:
        """Health Auto Export lands here.

        The bearer token identifies *which user* the payload belongs to. It
        used to be one shared token for the whole deployment, with the readings
        attributed to `select(User.id).limit(1)` — whichever row Postgres
        happened to return first. With two users that writes one person's steps
        and weight into the other's health series, and possession of the single
        token grants that against either of them.

        A miss is always 401 and never says which kind. Distinguishing "no such
        token" from "that user is not active" turns the endpoint into an oracle
        for enumerating tokens.

        The idempotency lives in ingest.health rather than here, so the tests
        cover it.
        """
        token = authorization.removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(status_code=401, detail="bad token")

        from umai.db.session import session_scope

        async with session_scope() as session:
            user_id = await _user_for_token(session, token)
            report = await ingest(session, user_id, payload)
        return JSONResponse(
            {
                "written": report.written,
                "duplicates": report.duplicates,
                "rejected": report.rejected,
                "summary": report.summary,
            }
        )

    @app.post("/webhook/telegram")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str = Header(default=""),
    ) -> dict:
        """Acknowledge first, work afterwards.

        This awaited `feed_update` inline once, which held the HTTP response
        open for however long the handler took. A photo takes 17-90s in the
        vision model, Telegram's webhook timeout is far shorter, so it hung up
        — the edge logged 499 at ~20s and ~60s — and then **redelivered the
        same update**. Every redelivery ran the whole pipeline again: on
        2026-08-26 one bowl of cacik became three live `log_entries` of 107,
        119 and 107 kcal, disagreeing with each other because perception ran
        three separate times, and all three counted toward the day.

        Media was never duplicated, because `_record_media` upserts on
        (user_id, sha256) — which is exactly why the bug looked contained when
        only the enrichment log was read.

        So the response is sent immediately and the dispatcher runs in a task.
        Nothing downstream wanted the return value: aiogram's
        answer-via-webhook shortcut is unused, every handler calls the Bot API
        directly.
        """
        if dispatcher is None or bot is None:
            raise HTTPException(status_code=404)
        expected = settings.telegram_webhook_secret
        if not expected or not hmac.compare_digest(x_telegram_bot_api_secret_token, expected):
            raise HTTPException(status_code=401, detail="bad secret")
        update = await request.json()
        if not isinstance(update, dict):
            raise HTTPException(status_code=400, detail="not an update")

        # Belt and braces behind the fast ack. Telegram also redelivers after a
        # network fault or a restart mid-handler, and an update_id it has
        # already been thanked for is never new work.
        update_id = update.get("update_id")
        if isinstance(update_id, int) and not app.state.seen_updates.add(update_id):
            log.info("webhook: ignoring redelivered update %s", update_id)
            return {"ok": True}

        _spawn(_feed(dispatcher, bot, update))
        return {"ok": True}

    return app


async def _feed(dispatcher, bot, update: dict) -> None:
    """Run one update to completion, off the request.

    Nothing awaits this, so an exception escaping it would otherwise surface
    only as asyncio's "task exception was never retrieved" at garbage-collection
    time, detached from the update that caused it.
    """
    try:
        await dispatcher.feed_update(bot=bot, update=Update(**update))
    except Exception:
        log.exception("webhook: update %s failed", update.get("update_id"))


# How long shutdown waits for detached update tasks before giving up on them.
DRAIN_TIMEOUT = 20.0

# Strong references to in-flight update tasks. asyncio holds only a weak one,
# so a task nobody keeps can be collected mid-await and the update silently
# vanishes.
_IN_FLIGHT: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)
    return task


class SeenUpdates:
    """The update ids already accepted, most recent `maxlen` of them.

    Bounded and in-process, which is the right shape for both constraints: the
    service runs exactly one replica — Telegram permits one webhook consumer
    per token — so there is no second process to share this with, and an
    unbounded set on a long-lived process is a slow leak. A restart forgets
    everything, which is acceptable because this is the second line of defence:
    the fast acknowledgement is what stops the redeliveries happening at all.
    """

    def __init__(self, maxlen: int = 2048) -> None:
        self._ids: set[int] = set()
        self._order: deque[int] = deque(maxlen=maxlen)

    def add(self, update_id: int) -> bool:
        """True if this id is new. False means it has been seen already."""
        if update_id in self._ids:
            return False
        if len(self._order) == self._order.maxlen and self._order:
            self._ids.discard(self._order[0])
        self._order.append(update_id)
        self._ids.add(update_id)
        return True


async def _user_for_token(session, token: str) -> uuid.UUID:
    """Whose health series this token writes into.

    Compared with `hmac.compare_digest` rather than by SQL equality on the
    token itself: an indexed `=` on a secret leaks its prefix through timing,
    and the index is small enough that scanning the active rows costs nothing.
    """
    from sqlalchemy import select

    from umai.db.models import User, UserStatus

    rows = (
        await session.execute(
            select(User.id, User.health_token).where(
                User.status == UserStatus.active,
                User.health_token.is_not(None),
            )
        )
    ).all()
    for user_id, stored in rows:
        if hmac.compare_digest(stored, token):
            return uuid.UUID(str(user_id))
    log.warning("health ingest rejected: unknown token")
    raise HTTPException(status_code=401, detail="bad token")
