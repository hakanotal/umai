"""/export and /delete_me — the two things you owe somebody whose health data
you are holding.

Umai stopped being a private notebook the moment a second person could use it.
The project plan already noted that health data is special-category data under
GDPR Article 9 and that the compliance burden is real; access and erasure are
the two obligations concrete enough to just build, so they are built.

The logic is in `core/datarights.py`. This module is the chat surface: two
files and a typed confirmation.

Deletion asks the person to type a word rather than tap a button. A button is
one mis-tap from irreversible, and this is the single most irreversible thing
the bot can do — every meal, every weight, every photo, with no undo and no
backup that belongs to them. Typing is friction on purpose.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, Message

from umai.config.settings import Settings
from umai.core import datarights
from umai.db.models import User
from umai.db.session import session_scope
from umai.telegram.middleware import Principal

log = logging.getLogger(__name__)

router = Router(name="datarights")

CONFIRM_WORD = "DELETE"


class Erasing(StatesGroup):
    """Waiting for the typed confirmation, and nothing else."""

    confirming = State()


@router.message(Command("export"))
async def export_command(message: Message, principal: Principal) -> None:
    """Everything Umai holds on you, as two files.

    Two rather than one because they answer different questions. The JSON is
    complete — it is what you would hand to something else that wanted to
    import your history. The CSV is legible — it is what you open when you want
    to look at what you have been eating.
    """
    async with session_scope() as session:
        data = await datarights.export(session, principal.id)

    payload = json.dumps(data, indent=2, ensure_ascii=False).encode()
    csv_bytes = datarights.entries_csv(data).encode()
    entries = len(data["entries"])

    await message.answer_document(
        BufferedInputFile(payload, filename="umai-export.json"),
        caption=(
            f"Everything I hold on you: {entries} entries, "
            f"{len(data['health_metrics'])} health readings, "
            f"{len(data['recipes'])} recipes, {len(data['photos'])} photos.\n\n"
            "Your health-sync token is deliberately not in here — it is an "
            "access key rather than information about you. /token shows it."
        ),
    )
    await message.answer_document(
        BufferedInputFile(csv_bytes, filename="umai-entries.csv"),
        caption="The same entries as a spreadsheet.",
    )


@router.message(Command("delete_me"))
async def delete_me(message: Message, state: FSMContext, principal: Principal) -> None:
    await state.set_state(Erasing.confirming)
    await message.answer(
        "This deletes everything: every meal, every weight, every photo, your "
        "profile, and your health-sync token. It cannot be undone and I keep no "
        "backup that belongs to you.\n\n"
        "Run /export first if you want a copy.\n\n"
        f"To go ahead, send {CONFIRM_WORD} in capitals. Anything else cancels."
    )


@router.message(Erasing.confirming)
async def confirm_delete(
    message: Message, state: FSMContext, principal: Principal, settings: Settings
) -> None:
    await state.clear()
    if (message.text or "").strip() != CONFIRM_WORD:
        await message.answer("Cancelled. Nothing was deleted.")
        return

    async with session_scope() as session:
        await datarights.erase(session, principal.id)

    # After the row, not before: a failed delete leaves the photos, which is
    # recoverable, where the reverse leaves rows pointing at files that are
    # gone. The directory is per user, which is what makes this one rm.
    _remove_media(settings, principal)

    log.info("erased user %s at their own request", principal.telegram_id)
    await message.answer(
        "Deleted. Nothing of yours is left.\n\n"
        "If you ever want to start again you will need the invite phrase."
    )


def _remove_media(settings: Settings, principal: Principal) -> None:
    """Their photo directory, best effort.

    Swallowing the error is deliberate: the database rows are already gone, and
    a permissions problem on disk must not leave the person believing the
    deletion failed when the part that holds their data succeeded. It is logged
    loudly instead, because it does need a human.
    """
    directory = Path(settings.media_dir) / str(principal.id)
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not remove media directory %s after erasing the user", directory)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


@router.message(Command("delete_user"))
async def delete_user(message: Message, principal: Principal, settings: Settings) -> None:
    """/delete_user <telegram_id>. The admin equivalent, for somebody who asked
    off-channel or who never had access to ask in chat.

    Deliberately separate from /block. Blocking revokes access and keeps the
    history, which is what you want for a stranger who guessed their way in;
    this destroys it, which is what you want when a person asks you to.
    """
    if not principal.is_admin:
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Send it as /delete_user <telegram id>.")
        return
    target = int(parts[1])
    if target == principal.telegram_id:
        await message.answer("Use /delete_me for your own account.")
        return

    async with session_scope() as session:
        from sqlalchemy import select

        user = (
            await session.execute(select(User).where(User.telegram_id == target))
        ).scalar_one_or_none()
        if user is None:
            await message.answer("No such user.")
            return
        user_id = user.id
        await datarights.erase(session, user_id)

    directory = Path(settings.media_dir) / str(user_id)
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not remove media directory %s", directory)

    log.info("admin %s erased user %s", principal.telegram_id, target)
    await message.answer(f"{target} and all their data are gone.")


@router.message(F.text.in_({"/export@umai", "/delete_me@umai"}))
async def _group_aliases(message: Message) -> None:  # pragma: no cover - defensive
    """Telegram appends @botname to commands in groups. The bot is not meant
    for groups, and an unhandled command there would fall through to the text
    catch-all and be logged as a meal called "/export@umai"."""
    await message.answer("Send that to me directly, not in a group.")
