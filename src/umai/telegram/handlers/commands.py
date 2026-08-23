"""The typed commands that only read: /start, /help, /summary, /today, /week, /edit.

/edit opens the same view as the ✏️ Edit today button; the buttons that act on
that view live in `edit.py`.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from umai.clock import Clock
from umai.config.settings import Settings
from umai.core import agent, tools
from umai.db.session import session_scope
from umai.telegram import keyboards
from umai.telegram.middleware import Principal

router = Router(name="commands")


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message) -> None:
    await message.answer(agent.HELP, reply_markup=keyboards.main_menu())


@router.message(Command("summary"))
@router.message(Command("today"))
async def today(message: Message, settings: Settings, clock: Clock, principal: Principal) -> None:
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        text = await agent.summary_line(session, user, clock, settings)
    await message.answer(text, reply_markup=keyboards.summary_actions())


@router.message(Command("week"))
async def week(message: Message, settings: Settings, clock: Clock, principal: Principal) -> None:
    async with session_scope() as session:
        user = await tools.load_user(session, principal.id)
        await message.answer(await agent.week_summary(session, user, clock))


@router.message(Command("edit"))
async def edit_command(
    message: Message, settings: Settings, clock: Clock, principal: Principal
) -> None:
    """The typed door into the edit flow. Same view as the ✏️ Edit today button."""
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
