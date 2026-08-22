"""Bot construction and startup.

Long polling in dev, webhook in prod: a branch at startup, not two code paths.
The allowlist middleware (TELEGRAM_ALLOWED_USER_IDS) runs before any handler,
from the first commit. A Telegram bot is discoverable by anyone who guesses
its username, and this one holds health data; unknown senders are ignored
silently rather than told why, so an outsider learns nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand, TelegramObject

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.perception.client import PerceptionClient
from umai.telegram.handlers import router

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="what I can do"),
    BotCommand(command="summary", description="where you are today"),
    BotCommand(command="week", description="the last seven days"),
    BotCommand(command="cuisines", description="what you usually eat"),
]


class AllowlistMiddleware(BaseMiddleware):
    """Rejects anyone not on TELEGRAM_ALLOWED_USER_IDS, before any handler.

    Silent by design: a reply to a stranger confirms the bot exists, is
    active, and cares about ids — three things an outsider should not learn.
    """

    def __init__(self, allowed: frozenset[int]) -> None:
        super().__init__()
        self._allowed = allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sender = data.get("event_from_user")
        if sender is None or sender.id not in self._allowed:
            log.warning("ignored update from unknown user %s", getattr(sender, "id", None))
            return None
        return await handler(event, data)


def build_dispatcher(settings: Settings, clock: Clock, models: ModelClient) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp["clock"] = clock
    dp["models"] = models
    dp["perception"] = PerceptionClient(models)
    # On the update observer, not per message type: an allowlist attached to
    # `message` and `callback_query` leaves any handler added later for
    # edited_message, inline_query or my_chat_member unguarded, and the one
    # thing this middleware may never be is forgettable.
    dp.update.outer_middleware(AllowlistMiddleware(settings.allowed_user_ids))
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
