"""The photo path: the four-stage pipeline, end to end.

Photo → vision model (items, state, grams, confidence only) → resolver →
pure-code arithmetic → one confirmation. The vision model is never asked for
macros.

Structured so that no database transaction is open across the vision call. The
first version held one for the whole 17-23 seconds, which on a five-connection
pool is a connection parked on a network round trip and a `logged_at`
(Postgres `now()`, i.e. transaction start) that predated the `occurred_at` it
was supposed to follow.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, PhotoSize
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.db.models import Media
from umai.db.session import session_scope
from umai.perception import images as image_tools
from umai.perception import prompt as prompt_mod
from umai.perception.client import PerceptionClient
from umai.telegram import keyboards
from umai.telegram.handlers.common import nudge_enrichment
from umai.telegram.middleware import Principal

log = logging.getLogger(__name__)

router = Router(name="photo")


@router.message(F.photo)
async def photo(
    message: Message,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    models: ModelClient,
    perception: PerceptionClient,
    principal: Principal,
) -> None:
    """The four-stage pipeline. The happy path is one reply, a few seconds.

    Structured so that no database transaction is open across the vision call.
    The first version held one for the whole 17-23 seconds, which on a
    five-connection pool is a connection parked on a network round trip and a
    `logged_at` (Postgres `now()`, i.e. transaction start) that predated the
    `occurred_at` it was supposed to follow.
    """
    # A photo answers whatever question was pending; leaving the state set means
    # the next plain number is eaten by the gram prompt instead of being logged.
    await state.clear()

    status: Message | None = await message.answer("Looking…")
    try:
        sizes = message.photo
        if not sizes:
            await message.answer("That photo looks empty — try again?")
            return
        path = await _download_with_retry(message, sizes[-1], settings, principal.id)
        if path is None:
            await message.answer("Couldn't fetch the photo from Telegram. Please try again.")
            return

        # 1. read what is needed for the prompt, then close the transaction.
        async with session_scope() as session:
            user = await tools.load_user(session, principal.id)
            user_id, tz, cuisines = user.id, user.zone, list(user.cuisines or [])
            ctx = prompt_mod.PromptContext(
                dinnerware=await tools.list_dinnerware(session, user_id),
                portion_priors=await tools.portion_priors(session, user_id),
                cuisines=cuisines,
                local_time=clock.now().astimezone(ZoneInfo(tz)).strftime("%A %H:%M"),
                note=message.caption or None,
            )
            media_id = await _record_media(session, user_id, path, tz)

        # 2. the slow part, with nothing held.
        try:
            outcome = await perception.analyse(path, ctx)
        except Exception:
            log.exception("perception failed")
            await message.answer("I couldn't read that photo. Another angle might help.")
            return

        # 3. resolve, write, reply.
        async with session_scope() as session:
            user = await tools.load_user(session, principal.id)
            logged = await agent.log_photo(
                session=session,
                models=models,
                user=user,
                clock=clock,
                outcome=outcome,
                media_id=media_id,
                caption=message.caption or None,
            )
        text = logged.text
        if outcome.result.clarifying_question:
            text += f"\n❓ {outcome.result.clarifying_question}"
        await message.answer(
            text,
            reply_markup=keyboards.meal_actions(str(logged.entry_id), len(outcome.result.items)),
        )
        if logged.needs_enrichment:
            # Nudge the background researcher rather than making the user wait
            # for it. Fire and forget by design: its failure is not this
            # message's problem, and the scheduled tick will pick the gap up.
            nudge_enrichment(models, clock)
    finally:
        if status is not None:
            with contextlib.suppress(Exception):
                await status.delete()


async def _record_media(
    session: AsyncSession, user_id: uuid.UUID, path: Path, tz: str
) -> uuid.UUID:
    """Insert the media row, or return this user's existing one for this file.

    The digest is unique **per user**, not globally. It was global, and the
    second person to photograph the same tin of beans hit
    `ON CONFLICT DO NOTHING`, got back the first person's row, and had their
    meal attached to a stranger's entry.

    The `entry_id` is filled in by `agent.log_photo` once the entry this
    produced exists. Both media rows from the first live session had a null
    one, which made "re-score this old photo" — the reason the file is kept at
    all — impossible.
    """
    digest = await asyncio.to_thread(image_tools.sha256_of, path)
    taken = await asyncio.to_thread(_taken_at_local, path, tz)
    stmt = (
        pg_insert(Media)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            path=str(path),
            sha256=digest,
            taken_at=taken,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "sha256"])
        .returning(Media.id)
    )
    media_id = (await session.execute(stmt)).scalar_one_or_none()
    if media_id is None:
        media_id = (
            await session.execute(
                select(Media.id).where(Media.user_id == user_id, Media.sha256 == digest)
            )
        ).scalar_one()
    return media_id


async def _download_with_retry(
    message: Message, photo: PhotoSize, settings: Settings, user_id: uuid.UUID
) -> Path | None:
    """Telegram file downloads are two calls (getFile then fetch) and both can
    fail independently, so the pair is retried together (gotcha list §12).

    One directory per user, and not only for tidiness. `file_unique_id` is
    stable per file per *bot*, not per user, so in a flat directory two people
    sending the same image resolve to the same path — and `if path.exists()`
    then hands the second one the first one's file. The bytes are identical so
    nothing leaks, but "delete everything you hold on me" would have deleted
    somebody else's photo. Per-user directories also make that deletion an
    `rm -rf` of one path.

    Files written before this change keep working: the row records an absolute
    path and nothing looks a photo up by recomputing its location.
    """
    bot = message.bot
    if bot is None:
        return None
    destination = Path(settings.media_dir) / str(user_id)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{photo.file_unique_id}.jpg"
    if path.exists():
        return path
    for attempt in range(2):
        try:
            await bot.download(photo, destination=path)
            return path
        except Exception:
            log.warning("photo download attempt %d failed", attempt + 1)
    return None


def _taken_at_local(path: Path, tz: str) -> dt.datetime | None:
    """EXIF carries naive camera-local time; attach the user's timezone here,
    in one place, then store UTC."""
    when = image_tools.taken_at(path)
    if when is None:
        return None
    return when.replace(tzinfo=ZoneInfo(tz)).astimezone(dt.UTC)
