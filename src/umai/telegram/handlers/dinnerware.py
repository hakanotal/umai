"""/dinnerware — the plates and bowls, measured once.

Measured once with a bank card beside the plate. The vision prompt uses these
as the primary scale reference, roughly halving portion error (papers A04).

No FSM: `name: description` is simple enough to parse from a single message.
"""

from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import tools
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.handlers.common import cb_data, cb_message
from umai.telegram.middleware import Principal

router = Router(name="dinnerware")


_DINNERWARE_BLURB = (
    "Your dinnerware, measured once so I can estimate portions from photos.\n\n"
    "To add: /dinnerware dinner plate: 26cm diameter\n"
    "To remove: tap the item below.\n\n"
    "How to measure: place a bank card (8.5cm) beside the item and compare, "
    "or use a tape measure. One measurement per item, remembered forever."
)


@router.message(Command("dinnerware"))
async def dinnerware_command(
    message: Message, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Manage dinnerware measurements.

    Free-text add via `/dinnerware name: description`, or view and remove
    existing items via the inline list. No FSM: the format is simple enough
    to parse from a single message.
    """
    text = message.text or ""
    # Strip the command prefix and any bot mention
    raw = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""

    if ":" in raw:
        # Add mode: "dinner plate: 26cm diameter"
        name, desc = raw.split(":", 1)
        name = name.strip()
        desc = desc.strip()
        if not name or not desc:
            await message.answer(
                "Format: /dinnerware name: description\n"
                "Example: /dinnerware dinner plate: 26cm diameter"
            )
            return
        async with session_scope() as session:
            user = await tools.load_user(session, principal.id)
            await tools.add_dinnerware(session, user.id, name, desc)
            items = await tools.list_dinnerware(session, user.id)
        await message.answer(f"Saved: {name}: {desc}\n\n" + _dinnerware_list_text(items))
        return

    # View mode
    await show_dinnerware_message(message, settings, clock, principal)


async def show_dinnerware_message(
    message: Message, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Show the dinnerware list, used by both the command and the configure callback."""
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        items = await tools.list_dinnerware(session, user.id)
    if not items:
        await message.answer(_DINNERWARE_BLURB)
        return
    await message.answer(
        _dinnerware_list_text(items),
        reply_markup=keyboards.dinnerware_list(items),
    )


async def show_dinnerware(
    callback: CallbackQuery, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Show the dinnerware list from a callback query (configure menu)."""
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        items = await tools.list_dinnerware(session, user.id)
    if not items:
        await callback.message.answer(_DINNERWARE_BLURB)  # type: ignore[union-attr]
        return
    await callback.message.answer(  # type: ignore[union-attr]
        _dinnerware_list_text(items),
        reply_markup=keyboards.dinnerware_list(items),
    )


def _dinnerware_list_text(items: dict[str, str]) -> str:
    if not items:
        return "No dinnerware saved yet."
    lines = "\n".join(f"  {k}: {v}" for k, v in items.items())
    return f"Your dinnerware:\n{lines}\n\nAdd more: /dinnerware name: description"


@router.callback_query(F.data.startswith("dw:"))
async def dinnerware_remove(
    callback: CallbackQuery, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """Remove a dinnerware item from the inline list."""
    name = cb_data(callback).split(":", 1)[1]
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        removed = await tools.remove_dinnerware(session, user.id, name)
        items = await tools.list_dinnerware(session, user.id)
    attached = cb_message(callback)
    if attached is not None:
        if items:
            with contextlib.suppress(Exception):
                await attached.edit_text(
                    _dinnerware_list_text(items),
                    reply_markup=keyboards.dinnerware_list(items),
                )
        else:
            with contextlib.suppress(Exception):
                await attached.edit_text("All dinnerware removed.")
    await callback.answer(f"Removed {name}" if removed else "Not found")
