"""Re-answering the onboarding questions, from the Configure menu.

The wizard asks seven questions once. Four of the answers — sex, height, birth
date and goal rate — are inputs to Mifflin-St Jeor and therefore to every
target the bot will ever show, and one of them, the timezone, decides where
every local day begins. Before this module the only way to change any of them
was `UPDATE users`, which is not a thing to ask of the person whose data it is.

**Nothing about what to ask lives here.** `core/onboarding.py` owns the prompt,
the parser and the display format for each field, and `editable_steps()` is
derived from `STEPS`, so a question added to the wizard becomes editable in the
same commit rather than in whichever later one somebody notices a second list.
This module draws keyboards, opens sessions and writes columns — the same job
`handlers/onboarding.py` does for the first pass.

**It does hold FSM state, and that is the difference from the wizard.** The
wizard can derive its current question from the first unset field, because
during onboarding exactly one prefix of the row is filled in. Here every field
is set, so "which one are we editing" is not recoverable from the data and has
to be remembered. The cost is the usual one: aiogram's default storage is
in-memory, so a restart mid-edit drops the pending answer. That is survivable
in a way the wizard's version was not — the row is complete and the user is
`active`, so an orphaned "180" falls through to `text.py` and is classified as
a meal, which is visible and one tap to delete, rather than trapping somebody
on a question forever.

**No field is ever cleared.** Only a parsed `Ok` is written, so a rejected
answer leaves the previous value in place. A profile editor that could null a
column would be able to violate `ck_users_active_has_tz` and, short of that,
to knock an `active` user into a state where `current_target` raises on every
summary.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from umai.clock import Clock
from umai.core import onboarding as ob
from umai.core import tools
from umai.db.models import User
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import cb_data, cb_message
from umai.telegram.middleware import Principal

log = logging.getLogger(__name__)

router = Router(name="profile")


class EditingProfile(StatesGroup):
    """Waiting for a typed answer to one named profile field.

    Separate from `common.Awaiting`, which is documented as "waiting for one
    number" and shared by grams, weigh-ins and the water target. A birth date
    is not a number, and widening that state to mean "waiting for anything"
    would make its handler guess which prompt an answer belongs to.
    """

    value = State()


MENU_TITLE = (
    "What I know about you. Tap anything to change it.\n\n"
    "Sex, height, date of birth and your goal all feed the daily target; "
    "your timezone decides when a day starts and ends."
)


def _menu_rows(user: User) -> list[tuple[str, str, str]]:
    """(field, label, current value) for every editable step, in wizard order."""
    return [
        (step.field, step.label, ob.describe(step, getattr(user, step.field, None)))
        for step in ob.editable_steps()
    ]


@router.callback_query(F.data == "cfg:profile")
async def show_profile(callback: CallbackQuery, state: FSMContext, principal: Principal) -> None:
    """Opening the menu abandons any half-finished edit.

    Without the clear, tapping Configure to escape a question and then typing
    an unrelated message would still have that message read as the answer.
    """
    await state.clear()
    await callback.answer()
    attached = cb_message(callback)
    if attached is None:
        return
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        rows = _menu_rows(user)
    await attached.answer(MENU_TITLE, reply_markup=keyboards.profile_menu(rows))


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def _keyboard(step: ob.Step) -> InlineKeyboardMarkup | None:
    """The wizard's keyboards, drawn with this router's callback prefix.

    Same `kind` tags and the same layouts, deliberately — a person who onboarded
    by tapping "Istanbul" should meet that button again rather than a bare
    prompt to type a city. Only the prefix differs, because the wizard's router
    is behind `NeedsGate()` and this one behind `IsActive()`.
    """
    if step.kind == "tz":
        return keyboards.profile_tz_regions()
    if step.kind == "sex":
        return keyboards.profile_sex()
    if step.kind == "goal":
        return keyboards.profile_goal(ob.GOAL_CHOICES)
    return None


@router.callback_query(F.data.startswith("prof:field:"))
async def edit_field(callback: CallbackQuery, state: FSMContext, principal: Principal) -> None:
    """Ask one question again, with the wizard's own wording.

    The full prompt rather than a terse "New height?": the sentence explaining
    that a birth date seeds the BMR is as much the reason to answer carefully
    the second time as the first, and someone who onboarded months ago has not
    seen it since.
    """
    field = cb_data(callback).split(":", 2)[2]
    step = ob.step_for(field)
    if step is None or step.field in ob.NOT_EDITABLE:
        # A stale keyboard from before a step was renamed or withdrawn.
        await callback.answer("That setting has moved.", show_alert=True)
        return
    await callback.answer()
    attached = cb_message(callback)
    if attached is None:
        return

    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        current = ob.describe(step, getattr(user, step.field, None))
        selected = list(user.cuisines or [])

    # Cuisines is a toggle grid that writes on every tap, so it has no pending
    # answer to remember and reuses the picker `/cuisines` already owns rather
    # than growing a second one here.
    if step.kind == "cuisines":
        await state.clear()
        await attached.answer(step.prompt, reply_markup=keyboards.cuisines(selected))
        return

    await state.set_state(EditingProfile.value)
    await state.update_data(field=step.field)
    await attached.answer(f"Currently {current}.\n\n{step.prompt}", reply_markup=_keyboard(step))


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


async def apply_edit(
    session: AsyncSession, user: User, step: ob.Step, value: object, clock: Clock
) -> str:
    """Write one field and describe the result, target included where it moved.

    Takes a session rather than opening one so it can be driven directly by a
    test — the same split `_record_media` uses. The caller owns the
    transaction; this writes into it and reads back through it.

    The target is recomputed here rather than left to the next summary because
    the whole reason to edit a height is to change a number the bot shows, and
    showing the old one until tomorrow morning reads as the edit having failed.
    """
    setattr(user, step.field, value)
    if step.field == "goal_rate_kg_per_week":
        # `goal_type` is derived, never asked. Letting it drift from the rate
        # would leave the two disagreeing with no question that could fix it.
        user.goal_type = ob.goal_type_for(float(value))  # type: ignore[arg-type]
    await session.flush()

    text = f"{step.label} is now {ob.describe(step, value)}."
    weight = await tools.latest_weight(session, user.id)
    if weight is None:
        return text
    try:
        decision = tools.current_target(user, weight, clock)
    except (RuntimeError, ValueError):
        # Incomplete or implausible body statistics. Not this function's
        # problem to report: the field the user just set was accepted, and the
        # summary says so in its own words.
        return text
    text += f"\nDaily target: {decision.kcal_target:.0f} kcal"
    if decision.clamped:
        text += " (held at the safety floor)"
    return text


async def _save(principal: Principal, step: ob.Step, value: object, clock: Clock) -> str:
    """`apply_edit` in a transaction of its own."""
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        text = await apply_edit(session, user, step, value, clock)
    log.info("profile: %s updated %s", principal.id, step.field)
    return text


async def _saved(message: Message, state: FSMContext, principal: Principal, text: str) -> None:
    """Confirm, drop the state, and put the menu back with the new value on it."""
    await state.clear()
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        rows = _menu_rows(user)
    await message.answer(text, reply_markup=keyboards.profile_menu(rows))


@router.message(EditingProfile.value, F.text)
async def value_received(
    message: Message, state: FSMContext, principal: Principal, clock: Clock
) -> None:
    assert message.text is not None  # guaranteed by the F.text filter
    field = (await state.get_data()).get("field")
    step = ob.step_for(field) if isinstance(field, str) else None
    if step is None:  # pragma: no cover - state set without a field
        await state.clear()
        return

    result = step.parse(message.text)
    if isinstance(result, ob.Retry):
        # The state is deliberately kept: a rejected answer means the question
        # is still owed, and clearing here would send the next attempt to the
        # meal classifier.
        await message.answer(result.message, reply_markup=_keyboard(step))
        return

    if step.field == "tz":
        await _offer_timezone(message, result.value, clock)
        return

    await _saved(message, state, principal, await _save(principal, step, result.value, clock))


# ---------------------------------------------------------------------------
# Button answers
# ---------------------------------------------------------------------------


async def _save_from_button(
    callback: CallbackQuery,
    state: FSMContext,
    principal: Principal,
    clock: Clock,
    field: str,
    value: object,
) -> None:
    step = ob.step_for(field)
    await callback.answer()
    attached = cb_message(callback)
    if step is None or attached is None:  # pragma: no cover
        return
    await _saved(attached, state, principal, await _save(principal, step, value, clock))


@router.callback_query(F.data.startswith("prof:set:sex:"))
async def pick_sex(
    callback: CallbackQuery, state: FSMContext, principal: Principal, clock: Clock
) -> None:
    value = cb_data(callback).rsplit(":", 1)[1]
    if value not in ("male", "female"):  # pragma: no cover - only our keyboard sends these
        await callback.answer()
        return
    await _save_from_button(callback, state, principal, clock, "sex", value)


@router.callback_query(F.data.startswith("prof:set:goal:"))
async def pick_goal(
    callback: CallbackQuery, state: FSMContext, principal: Principal, clock: Clock
) -> None:
    result = ob.parse_goal(cb_data(callback).rsplit(":", 1)[1])
    if isinstance(result, ob.Retry):  # pragma: no cover - only our keyboard sends these
        await callback.answer()
        return
    await _save_from_button(
        callback, state, principal, clock, "goal_rate_kg_per_week", result.value
    )


# ---------------------------------------------------------------------------
# Timezone, which is confirmed rather than accepted
# ---------------------------------------------------------------------------


async def _offer_timezone(message: Message, value: object, clock: Clock) -> None:
    if isinstance(value, list):
        await message.answer("Which one?", reply_markup=keyboards.profile_timezones(value[:8]))
        return
    await _confirm_timezone(message, str(value), clock)


async def _confirm_timezone(message: Message, zone: str, clock: Clock) -> None:
    """Echo the local time back and ask, exactly as the wizard does.

    A wrong zone is the one profile field that is invisible when wrong — it
    surfaces weeks later as a day that landed on the wrong date — so the extra
    tap is worth more here than anywhere else, and more on an edit than on the
    first answer: the user already has history that will re-bucket.
    """
    local = clock.now().astimezone(ZoneInfo(zone))
    await message.answer(
        f"{zone}. It's {local:%H:%M} there right now — is that right?",
        reply_markup=keyboards.profile_confirm_tz(zone),
    )


@router.callback_query(F.data.startswith("prof:tzregion:"))
async def pick_timezone_region(callback: CallbackQuery, clock: Clock) -> None:
    """One of the offered cities. Confirmed rather than written, like the rest.

    A tapped button is not more trustworthy than a typed city here: the whole
    risk with a zone is picking a plausible wrong one, and "London" is exactly
    as plausible to somebody in Dublin either way.
    """
    zone = cb_data(callback).split(":", 2)[2]
    await callback.answer()
    attached = cb_message(callback)
    if attached is not None:
        await _confirm_timezone(attached, zone, clock)


@router.callback_query(F.data == "prof:tzother")
async def ask_free_text_tz(callback: CallbackQuery) -> None:
    """The fallback for a city not on the grid.

    The FSM state is deliberately left alone: it is still the timezone question
    that is owed, and clearing it would send the typed city to the meal
    classifier instead.
    """
    await callback.answer()
    attached = cb_message(callback)
    if attached is not None:
        await attached.answer("Type your city name or the full zone name (like Europe/Istanbul).")


@router.callback_query(F.data.startswith("prof:tz:"))
async def pick_timezone(callback: CallbackQuery, clock: Clock) -> None:
    """One of several zones a typed city resolved to."""
    zone = cb_data(callback).split(":", 2)[2]
    await callback.answer()
    attached = cb_message(callback)
    if attached is not None:
        await _confirm_timezone(attached, zone, clock)


@router.callback_query(F.data == "prof:tzno")
async def reject_timezone(callback: CallbackQuery) -> None:
    await callback.answer()
    attached = cb_message(callback)
    if attached is not None:
        await attached.answer(
            "Then tell me a different city, or send the zone name directly (like Europe/Istanbul)."
        )


@router.callback_query(F.data.startswith("prof:tzok:"))
async def accept_timezone(
    callback: CallbackQuery, state: FSMContext, principal: Principal, clock: Clock
) -> None:
    zone = cb_data(callback).split(":", 2)[2]
    await _save_from_button(callback, state, principal, clock, "tz", zone)


__all__ = ["EditingProfile", "apply_edit", "router"]
