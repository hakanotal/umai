"""The free-text catch-all.

Registered last, deliberately: every other message router — the menu labels,
the recipe and number FSM states — must get first refusal, or a button tap
would cost a model call and a pending gram answer would be logged as a meal.
"""

from __future__ import annotations

import logging
import uuid

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.db.models import FoodItem
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import nudge_enrichment, sender_id

log = logging.getLogger(__name__)

router = Router(name="text")


@router.message(F.text)
async def text(message: Message, settings: Settings, clock: Clock, models: ModelClient) -> None:
    assert message.text is not None  # the F.text filter guarantees it
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, sender_id(message), clock=clock)
        try:
            reply = await agent.handle_text(
                session=session,
                models=models,
                user=user,
                clock=clock,
                text=message.text,
                settings=settings,
            )
        except Exception:
            log.exception("text handling failed")
            reply = agent.Reply("Something went wrong reading that. Please try again.")

    # A typed meal gets the same per-item correction keyboard a photographed
    # one gets. Without it "200g rice and chicken" was uncorrectable while the
    # reply suggested a /fix command that was never registered.
    #
    # Non-meal replies carry no keyboard at all: the persistent reply keyboard
    # already holds the quick actions, and repeating them inline under every
    # message made the chat a wall of duplicate buttons.
    markup = None
    if reply.entry_id is not None:
        async with session_scope() as session:
            count = await _item_count(session, reply.entry_id)
        markup = keyboards.meal_actions(str(reply.entry_id), count)
    await message.answer(reply.text, reply_markup=markup)
    if reply.needs_enrichment:
        nudge_enrichment(models, clock)


async def _item_count(session: AsyncSession, entry_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(FoodItem).where(FoodItem.entry_id == entry_id)
            )
        ).scalar_one()
    )
