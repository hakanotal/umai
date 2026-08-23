"""The persistent reply keyboard.

The buttons arrive as ordinary text. They are matched by exact label before the
intent classifier sees anything, which keeps the invariant that a button tap
never costs a model call. This router must therefore be included before the
free-text one, which would otherwise swallow the labels.

Library is NOT in MENU_LABELS — it is handled by its own router with a text
filter. The Configure button shows an inline keyboard with Recipe, Dinnerware
and Cuisines, each handled by its own callback.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.core import cuisines as cuisines_mod
from umai.db.models import EntryKind
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import Awaiting
from umai.telegram.middleware import Principal

router = Router(name="menu")


MENU_LABELS = {
    keyboards.WATER_250,
    keyboards.BTN_TODAY,
    keyboards.BTN_WEEK,
    keyboards.BTN_EDIT,
    keyboards.BTN_WEIGH,
    keyboards.BTN_CONFIGURE,
}


@router.message(F.text.in_(MENU_LABELS))
async def menu_button(
    message: Message, state: FSMContext, settings: Settings, clock: Clock, principal: Principal
) -> None:
    assert message.text is not None  # the F.text filter guarantees it
    label = message.text
    if label == keyboards.WATER_250:
        async with session_scope() as session:
            user = await tools.load_user(session, principal.id)
            await tools.log_simple(
                session,
                user.id,
                kind=EntryKind.water,
                value=250.0,
                unit="ml",
                occurred_at=clock.now(),
            )
            totals = await tools.day_totals(session, user, clock)
        wt = tools.water_target(user, settings)
        remaining = wt - totals.water_ml
        if remaining > 0:
            await message.answer(f"Water logged: 250 ml \U0001f4a7 ({remaining:.0f} ml to go)")
        else:
            await message.answer("Water logged: 250 ml \U0001f4a7 (target reached!)")
        return
    if label == keyboards.BTN_TODAY:
        async with session_scope() as session:
            user = await tools.load_user(session, principal.id)
            text = await agent.summary_line(session, user, clock, settings)
        await message.answer(text, reply_markup=keyboards.summary_actions())
        return
    if label == keyboards.BTN_WEEK:
        async with session_scope() as session:
            user = await tools.load_user(session, principal.id)
            await message.answer(await agent.week_summary(session, user, clock))
        return
    if label == keyboards.BTN_EDIT:
        async with session_scope() as session:
            user = await tools.load_user(session, principal.id)
            entries = await tools.today_entries(session, user, clock)
        if not entries:
            await message.answer("Nothing logged today yet.")
            return
        await message.answer(
            "Today's entries. Tap one to edit or remove it:",
            reply_markup=keyboards.edit_list(entries),
        )
        return
    if label == keyboards.BTN_CONFIGURE:
        await message.answer("Configure your setup:", reply_markup=keyboards.configure_menu())
        return
    # BTN_WEIGH
    await state.set_state(Awaiting.number)
    await state.update_data(mode="weight")
    await message.answer("Send me the number on the scale ⚖️")


@router.callback_query(F.data == "cfg:configure")
async def configure_callback(callback: CallbackQuery) -> None:
    """Re-show the configure inline keyboard."""
    await callback.answer()
    await callback.message.answer(  # type: ignore[union-attr]
        "Configure your setup:", reply_markup=keyboards.configure_menu()
    )


@router.callback_query(F.data == "cfg:recipe")
async def configure_recipe(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the recipe flow from the configure menu."""
    await callback.answer()
    await state.set_state(Awaiting.recipe_name)
    await callback.message.answer("What's the recipe name?")  # type: ignore[union-attr]


@router.callback_query(F.data == "cfg:dinnerware")
async def configure_dinnerware(
    callback: CallbackQuery, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Show the dinnerware list from the configure menu."""
    from umai.telegram.handlers.dinnerware import show_dinnerware

    await callback.answer()
    await show_dinnerware(callback, settings, clock, principal)


@router.callback_query(F.data == "cfg:cuisines")
async def configure_cuisines(
    callback: CallbackQuery, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Show the cuisine picker from the configure menu."""
    from umai.core import tools as tools_mod
    from umai.db.session import session_scope

    await callback.answer()
    async with session_scope() as session:
        user = await tools_mod.load_user(session, principal.id)
        selected = list(user.cuisines or [])
    await callback.message.answer(  # type: ignore[union-attr]
        _CUISINE_BLURB, reply_markup=keyboards.cuisines(selected)
    )


@router.callback_query(F.data == "cfg:water")
async def configure_water(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    clock: Clock,
    principal: Principal,
) -> None:
    """Ask for a new water target in ml."""
    await callback.answer()
    await state.set_state(Awaiting.number)
    await state.update_data(mode="water_target")
    current = None
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        current = user.water_target_ml
    if current is not None:
        hint = (
            f"Current target: {current:.0f} ml. Send a new number,"
            f' or "default" to reset to {settings.water_target_ml:.0f} ml.'
        )
    else:
        hint = (
            f"No custom target set (using {settings.water_target_ml:.0f} ml)."
            ' Send a number in ml, or "default" to keep the default.'
        )
    await callback.message.answer(hint)  # type: ignore[union-attr]


_CUISINE_BLURB = (
    "What do you usually eat? Tap to toggle.\n\n"
    "I hand this to the model that reads your photos, so it recognises the "
    f"dishes by name instead of describing them. Up to {cuisines_mod.MAX_CUISINES}."
)
