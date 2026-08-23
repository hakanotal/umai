"""Primitives shared by more than one handler module.

Everything here was previously a module-private helper in a single 1,100-line
handlers module. Splitting that file per feature made the sharing explicit:
these are the pieces two or more feature routers genuinely need, and nothing
else belongs here. A helper used by exactly one router stays in that router's
module, where its reasoning sits beside its only caller.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.db.models import EntryKind, LogEntry

log = logging.getLogger(__name__)


class Awaiting(StatesGroup):
    """The chat is waiting for one number: grams, a weigh-in, new ml, or a
    recipe ingredient."""

    number = State()
    recipe_ingredients = State()


def sender_id(message: Message) -> int:
    """from_user is Optional in aiogram's types; the allowlist middleware has
    already rejected anything without a sender, so this assert is for mypy."""
    assert message.from_user is not None
    return message.from_user.id


def cb_data(callback: CallbackQuery) -> str:
    assert callback.data is not None  # guarded by the F.data filters
    return callback.data


def cb_message(callback: CallbackQuery) -> Message | None:
    """The message a callback is attached to, if it is still accessible."""
    return callback.message if isinstance(callback.message, Message) else None


async def live_entry_by_prefix(
    session: AsyncSession, user_id: uuid.UUID, prefix: str
) -> LogEntry | None:
    """Resolve an 8-character prefix to a live entry of any editable kind.

    Scoped to this user: callback data is short by necessity (Telegram caps it
    at 64 bytes) and an unscoped prefix scan is a cross-user read waiting for
    the day a second user exists. Items are eager-loaded because the meal view
    reads them after the session's work is otherwise done.
    """
    stmt = (
        select(LogEntry)
        .options(selectinload(LogEntry.items))
        .where(
            LogEntry.user_id == user_id,
            LogEntry.kind.in_((EntryKind.food, EntryKind.drink, EntryKind.water)),
            LogEntry.superseded_by.is_(None),
        )
        .order_by(LogEntry.logged_at.desc())
        .limit(50)
    )
    for row in (await session.execute(stmt)).scalars():
        if str(row.id).startswith(prefix):
            return row
    return None


async def entry_by_prefix(
    session: AsyncSession, user_id: uuid.UUID, prefix: str
) -> uuid.UUID | None:
    """The same lookup narrowed to a meal, for the flows that can only edit one."""
    entry = await live_entry_by_prefix(session, user_id, prefix)
    if entry is None or entry.kind not in (EntryKind.food, EntryKind.drink):
        return None
    return entry.id


# Strong references to fire-and-forget tasks. Without this the event loop only
# holds a weak reference and the task can be garbage collected mid-flight.
_BACKGROUND: set[asyncio.Task[None]] = set()


def nudge_enrichment(models: ModelClient, clock: Clock) -> None:
    """Ask the enrichment job to run now rather than at its next tick.

    Deliberately not awaited and deliberately swallowing everything: the user's
    meal is already logged and their reply already sent, and a provider outage
    in a background researcher must not surface as a failed food log.
    """

    async def _run() -> None:
        from umai.core import enrichment
        from umai.db.session import get_factory

        try:
            report = await enrichment.enrich_once(get_factory(), models, clock)
            if report.did_work:
                log.info("enrichment nudge: %s", enrichment.summarise(report))
        except Exception:
            log.exception("enrichment nudge failed")

    task = asyncio.create_task(_run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
