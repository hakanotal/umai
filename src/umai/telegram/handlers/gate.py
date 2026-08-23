"""The door: the invite phrase, and the admin commands that manage who is in.

A stranger who finds the bot gets one thing — a request for the invite phrase.
Nothing else in the application is reachable until they produce it, and the
refusal says nothing about why: "that isn't it" and no more. A message that
explained the rule would tell someone who should not be here that there is a
rule, how long the phrase is, and that guessing is worth continuing.

Guessing is not worth continuing. Five wrong attempts and the row flips to
`blocked`, after which the bot is silent for that Telegram id forever and only
an admin can undo it. Without a counter a hidden phrase is an oracle that
answers several guesses a second, and the phrase is the only thing standing
between a stranger and someone else's health data.

Registered *before* the feature routers, and behind `NeedsGate` so it cannot
shadow anything for a user who is already active. See handlers/__init__.py for
why the order is load-bearing.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from sqlalchemy import func, select

from umai.config.settings import Settings
from umai.core import tokens
from umai.db.models import User, UserStatus
from umai.db.session import session_scope
from umai.telegram.filters import NeedsGate
from umai.telegram.middleware import Principal

log = logging.getLogger(__name__)

router = Router(name="gate")
router.message.filter(NeedsGate())
router.callback_query.filter(NeedsGate())

# Five, and then silence. Enough that a person who was told the phrase over the
# phone and mistyped it twice still gets in; far too few to search a phrase of
# UMAI_INVITE_CODE's minimum length.
MAX_CODE_ATTEMPTS = 5

ASK_FOR_PHRASE = (
    "This is a private food log. If you were invited, send me the phrase you were given."
)

WRONG_PHRASE = "That isn't it."

BLOCKED_ON_LAST_ATTEMPT = (
    "That isn't it. I'm not going to keep guessing with you — ask whoever "
    "invited you to sort it out."
)


@router.message(CommandStart())
async def start(message: Message, principal: Principal) -> None:
    """/start for someone who is not through the door yet.

    An onboarding user gets nothing here: the wizard's own router handles them,
    and it is registered after this one, so this handler explicitly stands
    aside rather than telling a half-onboarded person to send an invite phrase
    they have already used.
    """
    if principal.status == UserStatus.onboarding:
        return
    await message.answer(ASK_FOR_PHRASE)


@router.message(F.text)
async def redeem(message: Message, principal: Principal, settings: Settings) -> None:
    """Any text from a pending user is a guess at the phrase.

    Deliberately a catch-all rather than a `/join <phrase>` command. Somebody
    who has been handed a phrase types the phrase; asking them to remember a
    command as well is friction with no security value, since the phrase is the
    secret either way.
    """
    if principal.status != UserStatus.pending or not message.text:
        return

    if not tokens.phrase_matches(message.text, settings.invite_phrase):
        async with session_scope() as session:
            user = await session.get(User, principal.id)
            if user is None:  # pragma: no cover - deleted mid-conversation
                return
            user.code_attempts += 1
            attempts = user.code_attempts
            exhausted = attempts >= MAX_CODE_ATTEMPTS
            if exhausted:
                user.status = UserStatus.blocked
        log.warning(
            "wrong invite phrase from %s (attempt %s of %s)",
            principal.telegram_id,
            attempts,
            MAX_CODE_ATTEMPTS,
        )
        await message.answer(BLOCKED_ON_LAST_ATTEMPT if exhausted else WRONG_PHRASE)
        return

    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        user.status = UserStatus.onboarding
        user.admitted_at = func.now()
        user.code_attempts = 0
    log.info("admitted %s", principal.telegram_id)

    # The wizard's first question follows immediately, from its own router, so
    # that being let in and being asked something are one message apart rather
    # than one message and a silence.
    from umai.telegram.handlers import onboarding

    await onboarding.begin(message, principal)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
#
# A separate router, and *not* behind NeedsGate: an admin is an active user, so
# these would be unreachable from the gate router. It is registered among the
# feature routers instead. The `is_admin` check is carried per handler rather
# than as a router filter — there are three of them, the check is one line, and
# a filter would have to read the principal anyway.

admin_router = Router(name="gate-admin")


def _deny(principal: Principal) -> bool:
    return not principal.is_admin


@admin_router.message(Command("users"))
async def list_users(message: Message, principal: Principal) -> None:
    """Who is in, who is waiting, who is locked out."""
    if _deny(principal):
        return
    async with session_scope() as session:
        rows = (await session.execute(select(User).order_by(User.created_at).limit(50))).scalars()
        lines = [
            f"{u.telegram_id} — {u.status.value}"
            + (" (admin)" if u.is_admin else "")
            + (f", {u.tz}" if u.tz else "")
            for u in rows
        ]
    await message.answer("\n".join(lines) if lines else "Nobody yet.")


@admin_router.message(Command("block"))
async def block(message: Message, principal: Principal) -> None:
    """/block <telegram_id>. Revokes access without touching the history.

    Blocking rather than deleting, because entries are immutable and deleting
    the row would cascade every meal they ever logged. /delete_user is the
    other command, and it is deliberately not this one.
    """
    if _deny(principal):
        return
    await _set_status(message, principal, UserStatus.blocked, "blocked")


@admin_router.message(Command("unblock"))
async def unblock(message: Message, principal: Principal) -> None:
    """/unblock <telegram_id>. Also clears the wrong-phrase counter, or the
    user would be one guess from being blocked again."""
    if _deny(principal):
        return
    await _set_status(message, principal, UserStatus.onboarding, "unblocked")


async def _set_status(
    message: Message, principal: Principal, status: UserStatus, verb: str
) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await message.answer(f"Send it as /{verb.rstrip('ed')} <telegram id>.")
        return
    target = int(parts[1])
    if target == principal.telegram_id:
        await message.answer("Not yourself.")
        return

    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.telegram_id == target))
        ).scalar_one_or_none()
        if user is None:
            await message.answer("No such user.")
            return
        # An unblocked user whose profile is already complete goes straight back
        # to active; sending them through the wizard again would ask questions
        # their row can already answer.
        if status == UserStatus.onboarding and user.tz and user.sex and user.birth_date:
            status = UserStatus.active
        user.status = status
        user.code_attempts = 0
    await message.answer(f"{target} is now {status.value}.")
