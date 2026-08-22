"""Message, photo, voice and callback handlers."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, PhotoSize
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.core import cuisines as cuisines_mod
from umai.db.models import EntryKind, EntrySource, FoodItem, LogEntry, Media
from umai.db.session import session_scope
from umai.perception import images as image_tools
from umai.perception import prompt as prompt_mod
from umai.perception.client import PerceptionClient
from umai.telegram import keyboards

log = logging.getLogger(__name__)

router = Router(name="umai")


class Awaiting(StatesGroup):
    """The chat is waiting for one number: a gram value or a weigh-in."""

    number = State()


def _sender_id(message: Message) -> int:
    """from_user is Optional in aiogram's types; the allowlist middleware has
    already rejected anything without a sender, so this assert is for mypy."""
    assert message.from_user is not None
    return message.from_user.id


def _cb_data(callback: CallbackQuery) -> str:
    assert callback.data is not None  # guarded by the F.data filters
    return callback.data


def _cb_message(callback: CallbackQuery) -> Message | None:
    """The message a callback is attached to, if it is still accessible."""
    from aiogram.types import Message as _M

    return callback.message if isinstance(callback.message, _M) else None


# --- commands ----------------------------------------------------------------


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(agent.HELP, reply_markup=keyboards.quick_actions())


@router.message(Command("summary"))
@router.message(Command("today"))
async def today(message: Message, settings: Settings, clock: Clock) -> None:
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        await message.answer(await agent.summary_line(session, user, clock))


@router.message(Command("week"))
async def week(message: Message, settings: Settings, clock: Clock) -> None:
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        await message.answer(await agent.week_summary(session, user, clock))


@router.message(Command("cuisines"))
async def cuisines_command(message: Message, settings: Settings, clock: Clock) -> None:
    """Pick the cuisines you actually eat.

    Not a preference setting. The list is injected into the vision model's
    prompt, and it is the difference between "flatbread with reddish meat and
    pepper paste topping" — which matches nothing in a food table and was
    logged at zero calories — and "lahmacun", which is a lookup key and, failing
    that, something the enrichment job can research.
    """
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        selected = list(user.cuisines or [])
    await message.answer(_CUISINE_BLURB, reply_markup=keyboards.cuisines(selected))


_CUISINE_BLURB = (
    "What do you usually eat? Tap to toggle.\n\n"
    "I hand this to the model that reads your photos, so it recognises the "
    f"dishes by name instead of describing them. Up to {cuisines_mod.MAX_CUISINES}."
)


@router.callback_query(F.data.startswith("cuisine:"))
async def cuisine_toggle(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    slug = _cb_data(callback).split(":", 1)[1]
    attached = _cb_message(callback)
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        current = list(user.cuisines or [])
        if slug in current:
            current.remove(slug)
            note = f"{cuisines_mod.label(slug)} off"
        elif len(current) >= cuisines_mod.MAX_CUISINES:
            await callback.answer(
                f"{cuisines_mod.MAX_CUISINES} is the limit — a model told it eats "
                "everything has been told nothing. Untick one first.",
                show_alert=True,
            )
            return
        else:
            current.append(slug)
            note = f"{cuisines_mod.label(slug)} on"
        # Normalised on write so the prompt is stable across sessions: an
        # unstable prompt is an unstable fingerprint, and perception_runs
        # exists to tell prompt drift from model drift.
        user.cuisines = cuisines_mod.normalise(current)
        selected = list(user.cuisines)

    if attached is not None:
        with contextlib.suppress(Exception):  # unchanged markup is a 400
            await attached.edit_reply_markup(reply_markup=keyboards.cuisines(selected))
    await callback.answer(note)


@router.callback_query(F.data == "cuisine_done")
async def cuisine_done(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        selected = list(user.cuisines or [])
    attached = _cb_message(callback)
    named = ", ".join(cuisines_mod.label(s) for s in selected) or "nothing yet"
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text(f"Cooking with: {named}\n\nChange it any time with /cuisines.")
    await callback.answer()


# --- quick-action buttons ------------------------------------------------------


@router.callback_query(F.data.startswith("water:"))
async def water(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    ml = float(_cb_data(callback).split(":")[1])
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        await tools.log_simple(
            session,
            user.id,
            kind=EntryKind.water,
            value=ml,
            unit="ml",
            occurred_at=clock.now(),
        )
    await callback.answer(f"+{ml:.0f} ml 💧", show_alert=False)


@router.callback_query(F.data == "today")
async def today_button(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("Message is gone — send /summary instead.", show_alert=True)
        return
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        await attached.answer(await agent.summary_line(session, user, clock))
    await callback.answer()


@router.callback_query(F.data == "weigh")
async def weigh_button(callback: CallbackQuery, state: FSMContext) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("Message is gone — just send the number.", show_alert=True)
        return
    await state.set_state(Awaiting.number)
    await state.update_data(mode="weight")
    await attached.answer("Send me the number on the scale.")
    await callback.answer()


# --- meal confirmation ---------------------------------------------------------


@router.callback_query(F.data.startswith("fix:"))
async def fix_item(callback: CallbackQuery, state: FSMContext) -> None:
    attached = _cb_message(callback)
    if attached is None:
        await callback.answer("Too old to edit — log it fresh.", show_alert=True)
        return
    _, entry_prefix, item_no = _cb_data(callback).split(":")
    await state.set_state(Awaiting.number)
    await state.update_data(mode="grams", entry_prefix=entry_prefix, item_no=int(item_no))
    await attached.answer(f"How many grams was item {item_no}?")
    await callback.answer()


@router.callback_query(F.data.startswith("ok:"))
async def meal_ok(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Logged 👍")


@router.message(Awaiting.number, F.text)
async def number_received(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    data = await state.get_data()
    assert message.text is not None  # the F.text filter guarantees it
    try:
        value = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Just the number, please (or /summary to move on).")
        return

    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        if data.get("mode") == "weight":
            await tools.log_simple(
                session,
                user.id,
                kind=EntryKind.weight,
                value=value,
                unit="kg",
                occurred_at=clock.now(),
                source=EntrySource.button,
            )
            await state.clear()
            await message.answer(f"Logged {value:.1f} kg")
            return

        entry_id = await _entry_by_prefix(session, user.id, data["entry_prefix"])
        if entry_id is None:
            await state.clear()
            await message.answer("That meal is too old to edit — log it fresh.")
            return

        meal = await _fix_item_grams(session, entry_id, data["item_no"], value)
        if meal is None:
            await state.clear()
            await message.answer("I couldn't find that item — log the meal fresh.")
            return

        # The correction supersedes the entry, so the buttons under the old
        # message now point at a superseded id and every further fix would be
        # refused as "too old to edit". Re-send the meal with a live keyboard.
        text = tools.format_meal(meal)
        n_items = len(meal.items)
    await state.clear()
    await message.answer(text, reply_markup=keyboards.meal_actions(str(meal.entry.id), n_items))


async def _entry_by_prefix(
    session: AsyncSession, user_id: uuid.UUID, prefix: str
) -> uuid.UUID | None:
    """Resolve the 8-character prefix a callback carries back to an entry.

    Scoped to this user and to food entries: callback data is short by
    necessity (Telegram caps it at 64 bytes) and an unscoped prefix scan is a
    cross-user read waiting for the day a second user exists.
    """
    stmt = (
        select(LogEntry.id)
        .where(
            LogEntry.user_id == user_id,
            LogEntry.kind == EntryKind.food,
            LogEntry.superseded_by.is_(None),
        )
        .order_by(LogEntry.logged_at.desc())
        .limit(50)
    )
    for (id_,) in (await session.execute(stmt)).all():
        found: uuid.UUID = id_
        if str(found).startswith(prefix):
            return found
    return None


async def _fix_item_grams(
    session: AsyncSession, entry_id: uuid.UUID, item_no: int, grams: float
) -> tools.LoggedMeal | None:
    """Position order, the same order the confirmation message numbered.

    Returns the replacement meal, or None when the item number does not exist
    or the entry was already superseded — both of which used to be reported to
    the user as "Fixed." with nothing having changed.
    """
    items = (
        (
            await session.execute(
                select(FoodItem).where(FoodItem.entry_id == entry_id).order_by(FoodItem.position)
            )
        )
        .scalars()
        .all()
    )
    if not (1 <= item_no <= len(items)):
        return None
    return await tools.supersede_with_grams(session, entry_id, {items[item_no - 1].id: grams})


# --- the photo path --------------------------------------------------------------


@router.message(F.photo)
async def photo(
    message: Message,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    models: ModelClient,
    perception: PerceptionClient,
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
        path = await _download_with_retry(message, sizes[-1], settings)
        if path is None:
            await message.answer("Couldn't fetch the photo from Telegram — try again?")
            return

        # 1. read what is needed for the prompt, then close the transaction.
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            user_id, tz, cuisines = user.id, user.tz, list(user.cuisines or [])
            ctx = prompt_mod.PromptContext(
                dinnerware=await _dinnerware(session, user_id),
                portion_priors=await tools.portion_priors(session, user_id),
                cuisines=cuisines,
                local_time=clock.now().astimezone(ZoneInfo(tz)).strftime("%A %H:%M"),
            )
            media_id = await _record_media(session, path, tz)

        # 2. the slow part, with nothing held.
        try:
            outcome = await perception.analyse(path, ctx)
        except Exception:
            log.exception("perception failed")
            await message.answer("I couldn't read that photo — another angle might help.")
            return

        # 3. resolve, write, reply.
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, _sender_id(message), clock=clock
            )
            logged = await agent.log_photo(
                session=session,
                models=models,
                user=user,
                clock=clock,
                outcome=outcome,
                media_id=media_id,
            )
        text = logged.text
        if outcome.result.clarifying_question:
            text += f"\n❓ {outcome.result.clarifying_question}"
        await message.answer(
            text,
            reply_markup=keyboards.meal_actions(
                str(logged.entry_id), len(outcome.result.items)
            ),
        )
        if logged.needs_enrichment:
            # Nudge the background researcher rather than making the user wait
            # for it. Fire and forget by design: its failure is not this
            # message's problem, and the scheduled tick will pick the gap up.
            _nudge_enrichment(models, clock)
    finally:
        if status is not None:
            with contextlib.suppress(Exception):
                await status.delete()


async def _record_media(session: AsyncSession, path: Path, tz: str) -> uuid.UUID:
    """Insert the media row, or return the existing one for this file.

    sha256 is unique: the same photo sent twice is one media row, and the
    `entry_id` on it is filled in by `agent.log_photo` once the entry it
    produced exists. Both media rows from the first live session had a null
    entry_id, which made "re-score this old photo" — the reason the file is
    kept at all — impossible.
    """
    digest = await asyncio.to_thread(image_tools.sha256_of, path)
    taken = await asyncio.to_thread(_taken_at_local, path, tz)
    stmt = (
        pg_insert(Media)
        .values(id=uuid.uuid4(), path=str(path), sha256=digest, taken_at=taken)
        .on_conflict_do_nothing(index_elements=["sha256"])
        .returning(Media.id)
    )
    media_id = (await session.execute(stmt)).scalar_one_or_none()
    if media_id is None:
        media_id = (
            await session.execute(select(Media.id).where(Media.sha256 == digest))
        ).scalar_one()
    return media_id


def _nudge_enrichment(models: ModelClient, clock: Clock) -> None:
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


# Strong references to fire-and-forget tasks. Without this the event loop only
# holds a weak reference and the task can be garbage collected mid-flight.
_BACKGROUND: set[asyncio.Task[None]] = set()


async def _download_with_retry(
    message: Message, photo: PhotoSize, settings: Settings
) -> Path | None:
    """Telegram file downloads are two calls (getFile then fetch) and both can
    fail independently, so the pair is retried together (gotcha list §12)."""
    bot = message.bot
    if bot is None:
        return None
    destination = Path(settings.media_dir)
    destination.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - local disk, microseconds
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


async def _dinnerware(session: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    from umai.db.models import Dinnerware

    rows = (
        await session.execute(select(Dinnerware).where(Dinnerware.user_id == user_id))
    ).scalars()
    return {d.name: d.description for d in rows}


# --- free text -----------------------------------------------------------------


@router.message(F.text)
async def text(
    message: Message, settings: Settings, clock: Clock, models: ModelClient
) -> None:
    assert message.text is not None  # the F.text filter guarantees it
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, _sender_id(message), clock=clock)
        try:
            reply = await agent.handle_text(
                session=session, models=models, user=user, clock=clock, text=message.text
            )
        except Exception:
            log.exception("text handling failed")
            reply = agent.Reply("Something went wrong reading that — try again?")

    # A typed meal gets the same per-item correction keyboard a photographed
    # one gets. Without it "200g rice and chicken" was uncorrectable while the
    # reply suggested a /fix command that was never registered.
    if reply.entry_id is not None:
        async with session_scope() as session:
            count = await _item_count(session, reply.entry_id)
        markup = keyboards.meal_actions(str(reply.entry_id), count)
    else:
        markup = keyboards.quick_actions()
    await message.answer(reply.text, reply_markup=markup)
    if reply.needs_enrichment:
        _nudge_enrichment(models, clock)


async def _item_count(session: AsyncSession, entry_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(FoodItem).where(FoodItem.entry_id == entry_id)
            )
        ).scalar_one()
    )
