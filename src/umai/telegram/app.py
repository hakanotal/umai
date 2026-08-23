"""Bot construction and startup.

Long polling in dev, webhook in prod: a branch at startup, not two code paths.

Access is resolved before any handler by `AccessMiddleware`, which lives in
`telegram/middleware.py` — a Telegram bot is discoverable by anyone who guesses
its username, and this one holds health data. Strangers are not turned away
here, though: they reach the gate router, which asks for the invite phrase and
tells them nothing else. What keeps them out of the application is the
`IsActive()` filter on the parent router in `handlers/__init__.py`.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.perception.client import PerceptionClient
from umai.telegram.handlers import router
from umai.telegram.middleware import AccessMiddleware

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="what I can do"),
    BotCommand(command="help", description="what I can do"),
    BotCommand(command="summary", description="today's totals"),
    BotCommand(command="week", description="the last seven days"),
    BotCommand(command="edit", description="fix or remove today's entries"),
    BotCommand(command="library", description="frequent foods, one-tap log"),
    BotCommand(command="dinnerware", description="plate sizes for portion estimates"),
    BotCommand(command="recipe", description="save a dish you cook often"),
    BotCommand(command="cuisines", description="what you usually eat"),
    BotCommand(command="token", description="your Health Auto Export token"),
    BotCommand(command="export", description="download everything I hold on you"),
]


def build_dispatcher(settings: Settings, clock: Clock, models: ModelClient) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp["clock"] = clock
    dp["models"] = models
    dp["perception"] = PerceptionClient(models)
    # On the update observer, not per message type: a check attached to
    # `message` and `callback_query` leaves any handler added later for
    # edited_message, inline_query or my_chat_member unguarded, and the one
    # thing this middleware may never be is forgettable.
    dp.update.outer_middleware(AccessMiddleware(settings))
    dp.include_router(router)
    return dp


def build_bot(settings: Settings) -> Bot:
    """No default parse mode, deliberately.

    Replies carry food names straight from a vision model, and with HTML
    parsing on, one item called "fish & chips" makes Telegram reject the entire
    message with 400 "can't parse entities" — the meal written to the database
    and the user told nothing. Nothing in the Phase 1 replies needs markup, so
    the safe option is also the free one. Anything that later wants bold text
    passes parse_mode per message and escapes its own interpolations.
    """
    return Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
        session=AiohttpSession(timeout=60),  # vision calls take ~30s; keep the wire open
    )


async def run_polling(settings: Settings, bot: Bot, dispatcher: Dispatcher) -> None:
    """Dev mode: long polling, no public URL, one consumer per token."""
    log.info("starting long polling (dev)")
    await dispatcher.start_polling(
        bot,
        allowed_updates=dispatcher.resolve_used_update_types(),
        handle_signals=False,  # the composition root owns shutdown
    )
