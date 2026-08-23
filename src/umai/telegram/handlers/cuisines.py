"""/cuisines — the traditions this person actually eats.

Not a preference setting. The list is injected into the vision model's prompt,
and it is the difference between "flatbread with reddish meat and pepper paste
topping", which matches nothing in a food table and was logged at zero
calories, and "lahmacun", which is a lookup key and, failing that, something
the enrichment job can research.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import cuisines as cuisines_mod
from umai.core import tools
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import cb_data, cb_message, sender_id

router = Router(name="cuisines")


@router.message(Command("cuisines"))
async def cuisines_command(message: Message, settings: Settings, clock: Clock) -> None:
    """Pick the cuisines you actually eat.

    Not a preference setting. The list is injected into the vision model's
    prompt, and it is the difference between "flatbread with reddish meat and
    pepper paste topping", which matches nothing in a food table and was
    logged at zero calories, and "lahmacun", which is a lookup key and, failing
    that, something the enrichment job can research.
    """
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, sender_id(message), clock=clock)
        selected = list(user.cuisines or [])
    await message.answer(_CUISINE_BLURB, reply_markup=keyboards.cuisines(selected))


_CUISINE_BLURB = (
    "What do you usually eat? Tap to toggle.\n\n"
    "I hand this to the model that reads your photos, so it recognises the "
    f"dishes by name instead of describing them. Up to {cuisines_mod.MAX_CUISINES}."
)


@router.callback_query(F.data.startswith("cuisine:"))
async def cuisine_toggle(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    slug = cb_data(callback).split(":", 1)[1]
    attached = cb_message(callback)
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
    attached = cb_message(callback)
    named = ", ".join(cuisines_mod.label(s) for s in selected) or "nothing yet"
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text(f"Cooking with: {named}\n\nChange it any time with /cuisines.")
    await callback.answer()
