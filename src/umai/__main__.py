"""Process entrypoint.

Starts the Telegram bot and the FastAPI app (health ingest plus Mini App
backend) in one process. Long polling in dev, webhook in prod: a branch at
startup, not two code paths.

Analytics beyond the static Phase 1 target is deliberately absent: the
deployment target is a Raspberry Pi, and the calibration engine, correlations
and coach narration wait until the logging loop has real data to work with.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal

import structlog
import uvicorn

from umai.clock import SystemClock
from umai.config.models import ModelClient, preflight, refresh_prices
from umai.config.settings import get_settings
from umai.db.models import ApiUsage
from umai.db.session import dispose_engine, get_factory, init_engine, session_scope

log = structlog.get_logger(__name__)


async def _persist_usage(**kw) -> None:
    """Model spend lands in api_usage so the bot can report its own cost
    honestly. Best effort: a failed cost row must never take a user-facing
    call with it.

    Scheduled onto the main loop by `ModelClient._dispatch_usage`, from
    whichever thread made the call.
    """
    try:
        async with session_scope() as session:
            session.add(
                ApiUsage(
                    model=kw.get("model", "?"),
                    purpose=kw.get("task", "?"),
                    input_tokens=kw.get("tokens_in", 0),
                    output_tokens=kw.get("tokens_out", 0),
                    cost_usd=Decimal(str(round(kw.get("cost_usd", 0.0), 6))),
                    latency_ms=kw.get("latency_ms"),
                )
            )
    except Exception:
        logging.getLogger(__name__).warning("api_usage write failed", exc_info=True)


async def _send_summary(png: bytes, caption: str) -> None:
    """The scheduler's send hook: the evening summary as a photo into chat."""
    from aiogram.types import BufferedInputFile

    from umai.telegram.app import build_bot

    settings = get_settings()
    bot = build_bot(settings)
    try:
        await bot.send_photo(
            chat_id=next(iter(settings.allowed_user_ids)),
            photo=BufferedInputFile(png, filename="summary.png"),
            caption=caption,
        )
    finally:
        await bot.session.close()


async def amain() -> None:
    settings = get_settings()

    problems = settings.check_startup()
    if problems:
        for p in problems:
            log.error("startup.refused", problem=p)
        raise SystemExit("refusing to start; fix the problems above")

    refresh_prices()
    for issue in preflight():
        log.warning("models.preflight", issue=issue)

    init_engine(settings)
    models = ModelClient(api_key=settings.openrouter_api_key, on_usage=_persist_usage)
    # The usage callback is a coroutine and `_account` may run on a worker
    # thread, so the client needs to know which loop to schedule it on. Without
    # this the callback was created and dropped — "coroutine was never awaited"
    # — and api_usage stayed empty through a session of paid calls.
    models.bind_loop()

    from umai.telegram.app import COMMANDS, build_bot, build_dispatcher, run_polling

    bot = build_bot(settings)
    dispatcher = build_dispatcher(settings, SystemClock(), models)
    await bot.set_my_commands(COMMANDS)

    # --- the evening summary, idempotent via the job_runs claim ------------
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from umai.scheduler.jobs import schedule as schedule_jobs

    scheduler = AsyncIOScheduler()
    schedule_jobs(scheduler, get_factory(), settings, SystemClock(), _send_summary, models)
    scheduler.start()

    # --- http: health ingest (+ the webhook route in prod) ------------------
    from umai.web.api import create_app

    app = create_app(
        settings,
        dispatcher=dispatcher if settings.use_webhook else None,
        bot=bot if settings.use_webhook else None,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_level="warning")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        if settings.use_webhook:
            await bot.set_webhook(
                settings.telegram_webhook_url.rstrip("/") + "/webhook/telegram",
                secret_token=settings.telegram_webhook_secret,
                drop_pending_updates=False,
            )
            await server_task  # the webhook route feeds `dispatcher`
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            await run_polling(settings, bot, dispatcher)
    finally:
        scheduler.shutdown(wait=False)
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(server_task, timeout=5)
        await bot.session.close()
        await dispose_engine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(amain())


if __name__ == "__main__":
    main()
