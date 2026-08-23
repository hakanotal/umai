"""The onboarding wizard: seven questions, then the bot is yours.

Thin glue. What to ask, in what order, and how to read the answer all live in
`core/onboarding.py`, which has no Telegram and no database in it and is
therefore testable without either. This module draws the keyboards, writes the
column, and asks the next question.

**No FSM state.** The current question is `onboarding.next_step(user)`, derived
from the first field on the row that is still unset. That is the whole state
machine, and it means a restart mid-wizard resumes exactly where it was rather
than dropping somebody into the free-text catch-all, where their answer of
"180" is a perfectly plausible meal.

The weight answer is the exception that proves the rule: it has no column on
`users`, because weight belongs in the log rather than in a profile. It is held
on the row object only long enough to be written as a `LogEntry` at the very
end, so a wizard someone abandons halfway leaves no stray weight reading behind
to skew a trend that has no other points yet.
"""

from __future__ import annotations

import contextlib
import logging
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from umai.clock import Clock
from umai.core import cuisines as cuisines_mod
from umai.core import onboarding as ob
from umai.core import tokens, tools
from umai.db.models import EntryKind, EntrySource, User, UserStatus
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.filters import NeedsGate
from umai.telegram.handlers.common import cb_data
from umai.telegram.middleware import Principal

log = logging.getLogger(__name__)

router = Router(name="onboarding")
router.message.filter(NeedsGate())
router.callback_query.filter(NeedsGate())

# Where the answer to the weight question waits. Not a column: weight is a
# LogEntry, and writing it before the wizard finishes would leave an orphan
# reading behind if the person walked away.
WEIGHT_ATTR = ob.WEIGHT_FIELD

DONE = (
    "That's everything. Send me a photo of your next meal, or just type what "
    'you ate — "200g rice and chicken", "a coffee".'
)


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def _keyboard(step: ob.Step, user: User) -> InlineKeyboardMarkup | None:
    if step.kind == "sex":
        return keyboards.onboarding_sex()
    if step.kind == "goal":
        return keyboards.onboarding_goal(ob.GOAL_CHOICES)
    if step.kind == "cuisines":
        return keyboards.onboarding_cuisines(user.cuisines or [])
    return None


async def _ask_next(message: Message, user: User) -> bool:
    """Send the current question. False when there are none left."""
    step = ob.next_step(user)
    if step is None:
        return False
    n, total = ob.progress(user)
    prefix = "" if n == 1 else f"({n} of {total}) "
    await message.answer(prefix + step.prompt, reply_markup=_keyboard(step, user))
    return True


async def _advance(message: Message, principal: Principal, clock: Clock) -> None:
    """Ask the next question, or finish.

    Reloads the row rather than trusting a copy, because the caller has just
    written to it in a different session.
    """
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover - deleted mid-wizard
            return
        if await _ask_next(message, user):
            return
        await _finish(session, user, clock)
    await message.answer(DONE, reply_markup=keyboards.main_menu())


async def _finish(session: AsyncSession, user: User, clock: Clock) -> None:
    """Write the weight, mint the token, open the door.

    The order matters only in that `status` goes last: until it does, the row
    is still one the feature handlers refuse, so a failure anywhere above
    leaves a person who can be resumed rather than one who is `active` with a
    missing field and a target that can never be computed.
    """
    weight = getattr(user, WEIGHT_ATTR, None)
    if weight is not None:
        await tools.log_simple(
            session,
            user.id,
            kind=EntryKind.weight,
            value=float(weight),
            unit="kg",
            occurred_at=clock.now(),
            source=EntrySource.manual,
        )
    if not user.health_token:
        user.health_token = tokens.new_token()
    user.status = UserStatus.active
    log.info("onboarding complete for %s", user.telegram_id)


async def begin(message: Message, principal: Principal) -> None:
    """Called by the gate the moment someone is admitted, so that being let in
    and being asked something are one message apart rather than one message and
    a silence."""
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is not None:
            await _ask_next(message, user)


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


@router.message(Command("start"))
async def restart(message: Message, principal: Principal) -> None:
    """/start from someone mid-wizard re-asks the current question rather than
    starting over. Starting over would discard answers they have already given
    for no reason a person could have anticipated."""
    if principal.status != UserStatus.onboarding:
        return
    await begin(message, principal)


@router.message(F.text)
async def answer(message: Message, principal: Principal, clock: Clock) -> None:
    """A free-text answer to whichever question is currently owed."""
    if principal.status != UserStatus.onboarding or not message.text:
        return

    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        step = ob.next_step(user)
        if step is None:  # pragma: no cover - status says onboarding, row says done
            return
        result = step.parse(message.text)

        if isinstance(result, ob.Retry):
            await message.answer(result.message, reply_markup=_keyboard(step, user))
            return

        # The timezone is the one answer confirmed rather than accepted. It is
        # invisible when wrong, and everything the bot ever totals is computed
        # from it.
        if step.field == "tz":
            await _offer_timezone(message, result.value, clock)
            return

        setattr(user, step.field, result.value)
        if step.field == "goal_rate_kg_per_week":
            user.goal_type = ob.goal_type_for(float(result.value))

    await _advance(message, principal, clock)


async def _offer_timezone(message: Message, value: str | list[str], clock: Clock) -> None:
    """One candidate goes to the confirmation; several go to a keyboard."""
    if isinstance(value, list):
        await message.answer("Which one?", reply_markup=keyboards.onboarding_timezones(value[:8]))
        return
    await _confirm_timezone(message, value, clock)


async def _confirm_timezone(message: Message, zone: str, clock: Clock) -> None:
    """Echo the local time back and ask.

    The injected clock, not the wall clock, even though this is only a display
    string: the ban exists so that a test can put the bot at any instant, and a
    test of this confirmation is exactly the kind that wants to.
    """
    local = clock.now().astimezone(ZoneInfo(zone))
    await message.answer(
        f"{zone}. It's {local:%H:%M} there right now — is that right?",
        reply_markup=keyboards.onboarding_confirm_tz(zone),
    )


@router.callback_query(F.data.startswith("ob:tz:"))
async def pick_timezone(callback: CallbackQuery, clock: Clock) -> None:
    zone = cb_data(callback).split(":", 2)[2]
    await callback.answer()
    if isinstance(callback.message, Message):
        await _confirm_timezone(callback.message, zone, clock)


@router.callback_query(F.data == "ob:tzno")
async def reject_timezone(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Then tell me a different city, or send the zone name directly (like Europe/Istanbul)."
        )


@router.callback_query(F.data.startswith("ob:tzok:"))
async def accept_timezone(callback: CallbackQuery, principal: Principal, clock: Clock) -> None:
    zone = cb_data(callback).split(":", 2)[2]
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        user.tz = zone
    await callback.answer()
    if isinstance(callback.message, Message):
        await _advance(callback.message, principal, clock)


@router.callback_query(F.data.startswith("ob:sex:"))
async def pick_sex(callback: CallbackQuery, principal: Principal, clock: Clock) -> None:
    value = cb_data(callback).split(":", 2)[2]
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        user.sex = value
    await callback.answer()
    if isinstance(callback.message, Message):
        await _advance(callback.message, principal, clock)


@router.callback_query(F.data.startswith("ob:goal:"))
async def pick_goal(callback: CallbackQuery, principal: Principal, clock: Clock) -> None:
    rate = float(cb_data(callback).split(":", 2)[2])
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        user.goal_rate_kg_per_week = rate
        user.goal_type = ob.goal_type_for(rate)
    await callback.answer()
    if isinstance(callback.message, Message):
        await _advance(callback.message, principal, clock)


@router.callback_query(F.data.startswith("ob:cuisine:"))
async def toggle_cuisine(callback: CallbackQuery, principal: Principal) -> None:
    slug = cb_data(callback).split(":", 2)[2]
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        selected, note = cuisines_mod.toggle(list(user.cuisines or []), slug)
        if note is None:
            await callback.answer(
                f"{cuisines_mod.MAX_CUISINES} is the limit — a model told it eats "
                "everything has been told nothing. Untick one first.",
                show_alert=True,
            )
            return
        user.cuisines = selected

    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):  # unchanged markup is a 400
            await callback.message.edit_reply_markup(
                reply_markup=keyboards.onboarding_cuisines(selected)
            )
    await callback.answer(note)


@router.callback_query(F.data == "ob:cuisines_done")
async def finish_cuisines(callback: CallbackQuery, principal: Principal, clock: Clock) -> None:
    """Done ends the wizard, empty list or not.

    Cuisines is the last step, so this is the only exit — and it does not go
    through `next_step`, deliberately. An empty list reads as *unset* there,
    which is what makes the question appear for a new user; if finishing had to
    satisfy that test, somebody whose cooking is not on the list would be
    trapped on the last question of a wizard they could never complete. An
    empty list is a legitimate answer: it produces the neutral prompt.
    """
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        await _finish(session, user, clock)
    await callback.message.answer(DONE, reply_markup=keyboards.main_menu())


__all__ = ["begin", "router"]
