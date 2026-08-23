"""The persistent reply keyboard.

The buttons arrive as ordinary text. They are matched by exact label before the
intent classifier sees anything, which keeps the invariant that a button tap
never costs a model call. This router must therefore be included before the
free-text one, which would otherwise swallow the labels.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.db.models import EntryKind
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import Awaiting, sender_id

router = Router(name="menu")


# The buttons arrive as ordinary text. They are matched by exact label before
# the intent classifier sees anything, which keeps the invariant that a button
# tap never costs a model call.


MENU_LABELS = {
    keyboards.WATER_250,
    keyboards.WATER_500,
    keyboards.BTN_TODAY,
    keyboards.BTN_EDIT,
    keyboards.BTN_WEIGH,
}


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(
    message: Message, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    assert message.text is not None  # the F.text filter guarantees it
    label = message.text
    if label in (keyboards.WATER_250, keyboards.WATER_500):
        ml = 250.0 if label == keyboards.WATER_250 else 500.0
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, sender_id(message), clock=clock
            )
            await tools.log_simple(
                session,
                user.id,
                kind=EntryKind.water,
                value=ml,
                unit="ml",
                occurred_at=clock.now(),
            )
        await message.answer(f"Water logged: {ml:.0f} ml 💧")
        return
    if label == keyboards.BTN_TODAY:
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, sender_id(message), clock=clock
            )
            text = await agent.summary_line(session, user, clock)
        await message.answer(text, reply_markup=keyboards.summary_actions())
        return
    if label == keyboards.BTN_EDIT:
        async with session_scope() as session:
            user = await tools.get_or_create_user(
                session, settings, sender_id(message), clock=clock
            )
            entries = await tools.today_entries(session, user, clock)
        if not entries:
            await message.answer("Nothing logged today yet.")
            return
        await message.answer(
            "Today's entries. Tap one to edit or remove it:",
            reply_markup=keyboards.edit_list(entries),
        )
        return
    # BTN_WEIGH
    await state.set_state(Awaiting.number)
    await state.update_data(mode="weight")
    await message.answer("Send me the number on the scale ⚖️")
