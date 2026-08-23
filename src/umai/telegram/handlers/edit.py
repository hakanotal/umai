"""Editing and removing today's entries.

Entries are immutable, so nothing here rewrites one in place: a gram fix
supersedes, and the delete button is a genuine hard delete of a mistake that
should never have been a row. Every callback re-reads the entry rather than
trusting the id baked into an old keyboard, because that keyboard may be
pointing at something already superseded.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import tools
from umai.db.models import EntryKind
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import (
    Awaiting,
    cb_data,
    cb_message,
    live_entry_by_prefix,
)

router = Router(name="edit")


@router.callback_query(F.data == "editlist")
async def edit_list_open(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    attached = cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        entries = await tools.today_entries(session, user, clock)
    if not entries:
        await callback.answer("Nothing logged today yet.", show_alert=True)
        return
    with contextlib.suppress(Exception):  # unchanged markup is a 400
        await attached.edit_text(
            "Today's entries. Tap one to edit or remove it:",
            reply_markup=keyboards.edit_list(entries),
        )
    await callback.answer()


@router.callback_query(F.data == "editclose")
async def edit_list_close(callback: CallbackQuery) -> None:
    attached = cb_message(callback)
    if attached is not None:
        with contextlib.suppress(Exception):
            await attached.edit_text("Closed. Tap ✏️ Edit today any time.")
    await callback.answer()


@router.callback_query(F.data.startswith("edit:"))
async def edit_entry_open(
    callback: CallbackQuery, state: FSMContext, settings: Settings, clock: Clock
) -> None:
    """The per-entry view: a meal gets per-item gram fixes and removal,
    water gets a new amount and removal."""
    attached = cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    await state.clear()
    prefix = cb_data(callback).split(":", 1)[1]
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        entry = await live_entry_by_prefix(session, user.id, prefix)
        if entry is None:
            await callback.answer("That entry is gone. Tap ↩ Back for the list.", show_alert=True)
            return
        text = tools.format_entry(user, entry)
        if entry.kind is EntryKind.water:
            markup = keyboards.edit_water(prefix)
        else:
            markup = keyboards.edit_meal(prefix, len(entry.items))
    with contextlib.suppress(Exception):
        await attached.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("del:"))
async def delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    attached = cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    await state.clear()
    prefix = cb_data(callback).split(":", 1)[1]
    with contextlib.suppress(Exception):
        await attached.edit_text(
            "Remove this entry? This cannot be undone.",
            reply_markup=keyboards.delete_confirm(prefix),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("delyes:"))
async def delete_confirm(callback: CallbackQuery, settings: Settings, clock: Clock) -> None:
    attached = cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    prefix = cb_data(callback).split(":", 1)[1]
    async with session_scope() as session:
        user = await tools.get_or_create_user(session, settings, callback.from_user.id, clock=clock)
        entry = await live_entry_by_prefix(session, user.id, prefix)
        if entry is not None:
            await tools.hard_delete_entry(session, user.id, entry.id)
    with contextlib.suppress(Exception):
        await attached.edit_text("Removed ✅")
    await callback.answer("Removed")


@router.callback_query(F.data.startswith("waterfix:"))
async def water_fix(callback: CallbackQuery, state: FSMContext) -> None:
    attached = cb_message(callback)
    if attached is None:
        await callback.answer("This list is out of date. Tap ✏️ Edit today again.", show_alert=True)
        return
    prefix = cb_data(callback).split(":", 1)[1]
    await state.set_state(Awaiting.number)
    await state.update_data(mode="water_edit", entry_prefix=prefix)
    await attached.answer("Send me the new amount in ml.")
    await callback.answer()
