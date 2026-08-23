"""Who is this, and are they allowed in.

Runs before every handler, on the update observer rather than per message type,
so a handler added later for `edited_message` or `my_chat_member` is guarded by
the same code without anyone having to remember. The one thing this may never
be is forgettable.

It replaces an allowlist of Telegram ids read from the environment. That worked
for exactly one person: admitting a second meant editing `.env` and restarting,
and every handler then had to resolve the sender to a row for itself, twenty-five
times over. Access state now lives on the row (`users.status`) and is resolved
once, here.

**It does not hold the session open across the handler.** The obvious shape —
`async with session_scope(): return await handler(...)` — would park a
connection for the whole of a photo handler, which spends 17 to 23 seconds
inside a vision model, on a pool of five. Four photos and the bot stops
answering anybody. It is also the thing the project's "no database transaction
may be held across a model call" rule exists to prevent. So this opens a
session, resolves the row, commits, closes, and hands the handler a snapshot.

The snapshot is a frozen dataclass rather than the `User` row. The row would
technically survive the session (`expire_on_commit=False`), but as a detached
ORM object it invites a handler to assign to it, and the assignment would be
silently discarded. `Principal` cannot be assigned to at all.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from umai.config.settings import Settings
from umai.db.models import User, UserStatus
from umai.db.session import session_scope

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Principal:
    """The sender, as far as any handler needs to know.

    Everything here is a scalar copied out of the row while a session was open.
    A handler that needs more opens its own session and loads the row by `id`,
    which is a primary-key lookup.
    """

    id: uuid.UUID
    telegram_id: int
    status: UserStatus
    tz: str | None
    is_admin: bool

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.active


class AccessMiddleware(BaseMiddleware):
    """Resolve the sender to a `Principal`, or drop the update.

    Only two things are dropped outright: an update with no sender at all, and
    one from a blocked user. Everything else is admitted *as far as the gate* —
    a pending user has to reach the gate router to type the invite phrase, and
    an onboarding user has to reach the wizard. The `IsActive` filter in
    `telegram/filters.py` is what keeps them out of the feature handlers.

    A blocked user gets silence rather than a refusal. Telling someone they are
    blocked confirms the bot exists, is running, and remembers them; silence
    tells them nothing at all.
    """

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sender = data.get("event_from_user")
        if sender is None:
            return None

        async with session_scope() as session:
            user = await ensure_user(
                session,
                sender.id,
                bootstrap_admin_id=self._settings.bootstrap_admin_telegram_id,
            )
            principal = Principal(
                id=user.id,
                telegram_id=user.telegram_id,
                status=user.status,
                tz=user.tz,
                is_admin=user.is_admin,
            )

        if principal.status == UserStatus.blocked:
            log.info("ignored update from blocked user %s", principal.telegram_id)
            return None

        data["principal"] = principal
        return await handler(event, data)


async def ensure_user(
    session: AsyncSession,
    telegram_id: int,
    *,
    bootstrap_admin_id: int | None = None,
) -> User:
    """The row for this Telegram id, creating a pending one if there is none.

    **Seeds nothing.** Its predecessor copied timezone, sex, height, birth date,
    goal rate, cuisines and a starting weight out of the environment onto every
    row it created, which made the second person to use the bot a clone of the
    first: their BMR, their safety floors and their local day all belonged to
    someone else. Those fields now arrive from the onboarding wizard, per person.

    The one exception is the bootstrap admin, whose id is configured. They skip
    the invite phrase, because otherwise the first run of a fresh deployment has
    nobody who can admit anybody — including themselves.

    Racy by nature: two updates from a new user can arrive together, both find
    no row, and both insert. The unique index on `telegram_id` decides, and the
    loser re-reads rather than failing, which is the same shape as the
    `ON CONFLICT` used elsewhere.
    """
    stmt = select(User).where(User.telegram_id == telegram_id)
    user = (await session.execute(stmt)).scalar_one_or_none()
    if user is not None:
        return user

    is_bootstrap = bootstrap_admin_id is not None and telegram_id == bootstrap_admin_id
    user = User(
        telegram_id=telegram_id,
        status=UserStatus.onboarding if is_bootstrap else UserStatus.pending,
        is_admin=is_bootstrap,
        cuisines=[],
    )
    if is_bootstrap:
        # func.now() rather than a Clock: this is a database-side default for a
        # row being inserted right now, and routing it through the injected
        # clock would buy testability the tests do not want while adding a
        # parameter to every caller.
        user.admitted_at = func.now()
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return (await session.execute(stmt)).scalar_one()
    return user
