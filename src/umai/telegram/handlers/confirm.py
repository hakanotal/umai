"""The one confirmation that closes the four-stage pipeline.

Stage 4: the user either taps 👍 or corrects a portion. A correction supersedes
the entry, so the reply carries a fresh keyboard pointing at the new id — the
buttons under the old message now address a superseded row, and every further
fix would otherwise be refused as "too old to edit".

This router also owns `Awaiting.number`, the single numeric prompt shared by
gram fixes, weigh-ins and water edits.
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from umai.analytics import safety
from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import tools
from umai.db.models import EntryKind, EntrySource, FoodItem, LogEntry
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import (
    Awaiting,
    cb_data,
    cb_message,
    entry_by_prefix,
    live_entry_by_prefix,
)
from umai.telegram.middleware import Principal

router = Router(name="confirm")


@router.callback_query(F.data.startswith("fix:"))
async def fix_item(callback: CallbackQuery, state: FSMContext) -> None:
    attached = cb_message(callback)
    if attached is None:
        await callback.answer(
            "Too old to edit. Log it fresh, or try ✏️ Edit today.", show_alert=True
        )
        return
    _, entry_prefix, item_no = cb_data(callback).split(":")
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
    message: Message, state: FSMContext, settings: Settings, clock: Clock, principal: Principal
) -> None:
    data = await state.get_data()
    assert message.text is not None  # the F.text filter guarantees it
    try:
        value = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("Just the number, please. Or tap ✏️ Edit today to pick something else.")
        return

    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        if data.get("mode") == "weight":
            if not safety.is_plausible_weight(value):
                await message.answer(
                    f"{value} doesn't look like a body weight. Try again, or send something else."
                )
                return
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
            await message.answer(f"Logged {value:.1f} kg ⚖️")
            return

        if data.get("mode") == "water_edit":
            if value <= 0:
                await message.answer(
                    "The amount needs to be above zero. Send a number in ml, or tap ↩ Back."
                )
                return
            entry = await live_entry_by_prefix(session, user.id, data["entry_prefix"])
            if entry is None or entry.kind is not EntryKind.water:
                await state.clear()
                await message.answer("That water entry is gone. Tap ✏️ Edit today for the list.")
                return
            new_entry = await tools.edit_water(session, user.id, entry.id, value)
            if new_entry is None:
                await state.clear()
                await message.answer("That water entry is gone. Tap ✏️ Edit today for the list.")
                return
            await state.clear()
            await message.answer(f"Water updated: {value:.0f} ml 💧")
            return

        if data.get("mode") == "water_target":
            if value <= 0:
                user.water_target_ml = None
                await session.commit()
                await state.clear()
                await message.answer(
                    f"Water target reset to default ({settings.water_target_ml:.0f} ml) 💧"
                )
                return
            user.water_target_ml = value
            await session.commit()
            await state.clear()
            await message.answer(f"Water target set to {value:.0f} ml per day 💧")
            return

        entry_id = await entry_by_prefix(session, user.id, data["entry_prefix"])
        if entry_id is None:
            await state.clear()
            await message.answer("That meal is too old to edit. Log it fresh, or tap ✏️ Edit today.")
            return

        meal = await _fix_item_grams(session, user.id, entry_id, data["item_no"], value)
        if meal is None:
            await state.clear()
            await message.answer("I couldn't find that item. Tap ✏️ Edit today for the list.")
            return

        # The correction supersedes the entry, so the buttons under the old
        # message now point at a superseded id and every further fix would be
        # refused as "too old to edit". Re-send the meal with a live keyboard.
        text = tools.format_meal(meal)
        n_items = len(meal.items)
    await state.clear()
    await message.answer(text, reply_markup=keyboards.meal_actions(str(meal.entry.id), n_items))


async def _fix_item_grams(
    session: AsyncSession,
    user_id: uuid.UUID,
    entry_id: uuid.UUID,
    item_no: int,
    grams: float,
) -> tools.LoggedMeal | None:
    """Position order, the same order the confirmation message numbered.

    Returns the replacement meal, or None when the item number does not exist
    or the entry was already superseded — both of which used to be reported to
    the user as "Fixed." with nothing having changed.

    The item query joins back to `log_entries` for the ownership predicate.
    `food_items` carries no `user_id` of its own — it is scoped through its
    entry — so filtering on `entry_id` alone trusts that the caller checked,
    and the entry id came out of callback data.
    """
    items = (
        (
            await session.execute(
                select(FoodItem)
                .join(LogEntry, FoodItem.entry_id == LogEntry.id)
                .where(FoodItem.entry_id == entry_id, LogEntry.user_id == user_id)
                .order_by(FoodItem.position)
            )
        )
        .scalars()
        .all()
    )
    if not (1 <= item_no <= len(items)):
        return None
    return await tools.supersede_with_grams(
        session, user_id, entry_id, {items[item_no - 1].id: grams}
    )
