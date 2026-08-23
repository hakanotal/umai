"""/token — the bearer credential Health Auto Export posts with.

One token per user, and it is what tells the ingest endpoint whose step count
and whose weight a payload is. Before multi-user there was a single
deployment-wide token and the readings were attributed to whichever row
Postgres returned first, which with two people writes one person's health data
into the other's series.

Stored in plaintext on the row, deliberately. The alternative — a hash, shown
once at generation — means a lost token is a rotation and a re-paste into a
phone app, and on a self-hosted deployment where the database is the operator's
own that trade buys very little. The consequence is that anyone who can read
the table can post as any user, which is already true of anyone who can read
the table.

The message is built to be pasted: Health Auto Export wants a URL and a header,
and splitting them across two messages means one of them gets lost.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from umai.config.settings import Settings
from umai.core import tokens
from umai.db.models import User
from umai.db.session import session_scope
from umai.telegram.middleware import Principal

router = Router(name="token")


def _rotate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 New token", callback_data="token:rotate")]]
    )


def _endpoint(settings: Settings) -> str:
    """Where the phone posts.

    In prod the webhook URL is the public origin, so the ingest path hangs off
    it. In dev there is no public origin at all — the endpoint is bound to
    loopback and reached through `tailscale serve` — so the message says so
    rather than printing a localhost URL that would never work from a phone.
    """
    base = settings.telegram_webhook_url.rstrip("/")
    if base:
        return f"{base}/ingest/health"
    return "https://<your tailscale name>/ingest/health"


def _message(settings: Settings, token: str) -> str:
    return (
        "Health Auto Export → Automations → REST API.\n\n"
        f"URL:\n{_endpoint(settings)}\n\n"
        "Header:\n"
        f"Authorization: Bearer {token}\n\n"
        "Send steps and weight as JSON. Anyone holding this token can write to "
        "your health data, so treat it like a password — and tap below if you "
        "ever need to invalidate it."
    )


async def _ensure_token(user: User) -> str:
    """Mint one if the row somehow has none.

    Onboarding generates it, so this only fires for a row that predates
    multi-user and whose migration found no HEALTH_INGEST_TOKEN to inherit.
    """
    if not user.health_token:
        user.health_token = tokens.new_token()
    return user.health_token


@router.message(Command("token"))
async def show_token(message: Message, principal: Principal, settings: Settings) -> None:
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        token = await _ensure_token(user)
    await message.answer(_message(settings, token), reply_markup=_rotate_keyboard())


@router.callback_query(F.data == "cfg:token")
async def token_from_menu(
    callback: CallbackQuery, principal: Principal, settings: Settings
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        token = await _ensure_token(user)
    await callback.message.answer(_message(settings, token), reply_markup=_rotate_keyboard())


@router.callback_query(F.data == "token:rotate")
async def rotate(callback: CallbackQuery, principal: Principal, settings: Settings) -> None:
    """Invalidate the old token by replacing it.

    Immediate and without a confirmation step: the cost of rotating by accident
    is re-pasting a header, and the cost of hesitating over a leaked credential
    is somebody else's writes in your health series.
    """
    async with session_scope() as session:
        user = await session.get(User, principal.id)
        if user is None:  # pragma: no cover
            return
        user.health_token = tokens.new_token()
        token = user.health_token
    await callback.answer("Old token invalidated.")
    if isinstance(callback.message, Message):
        await callback.message.answer(_message(settings, token), reply_markup=_rotate_keyboard())
