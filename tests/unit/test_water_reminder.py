"""Water reminder scheduler job tests.

Tests the escalating-interval logic by mocking tools.day_totals,
tools.water_target, and the claim function — no real database needed.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from umai.scheduler.jobs import (
    _WATER_REMINDER_MESSAGES,
    water_reminder,
)


def _make_settings(water_target_ml=2500.0):
    return SimpleNamespace(water_target_ml=water_target_ml)


def _make_totals(water_ml=0.0):
    return SimpleNamespace(water_ml=water_ml)


def _noop_totals():
    return _make_totals(water_ml=0)


class FakeClock:
    def __init__(self, now: dt.datetime):
        self._now = now

    def now(self):
        return self._now


class _CtxManager:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *a):
        pass


class FakeSession:
    def __init__(self, user=None, count=0):
        self._user = user
        self._count = count

    async def execute(self, stmt):
        return SimpleNamespace(
            scalar_one_or_none=lambda: self._user,
            scalar_one=lambda: self._count,
        )


def _session_factory(session=None):
    if session is None:
        session = FakeSession()

    def factory():
        return _CtxManager(session)

    return factory


# Mirrors the real row: production reads the zone through User.zone, which
# exists because users.tz is nullable until onboarding finishes. A stub that
# only carried .tz would pass while the code under test could not run.
_USER = SimpleNamespace(tz="Europe/Istanbul", zone="Europe/Istanbul")

# Common patch decorators extracted for readability.
_totals_zero = patch(
    "umai.scheduler.jobs.tools.day_totals",
    new_callable=AsyncMock,
    return_value=_noop_totals(),
)
_wtarget = patch(
    "umai.scheduler.jobs.tools.water_target",
    return_value=2500.0,
)
_totals_500 = patch(
    "umai.scheduler.jobs.tools.day_totals",
    new_callable=AsyncMock,
    return_value=_make_totals(water_ml=500),
)


@pytest.mark.asyncio
@_totals_zero
@_wtarget
async def test_no_user_does_nothing(mock_wt, mock_dt):
    session = FakeSession(user=None)
    send = AsyncMock()
    await water_reminder(
        _session_factory(session),
        _make_settings(),
        FakeClock(dt.datetime.now()),
        send,
    )
    send.assert_not_called()
    mock_dt.assert_not_called()


@pytest.mark.asyncio
@patch(
    "umai.scheduler.jobs.tools.day_totals",
    new_callable=AsyncMock,
    return_value=_make_totals(water_ml=2500),
)
@_wtarget
async def test_target_met_does_not_remind(mock_wt, mock_dt):
    send = AsyncMock()
    await water_reminder(
        _session_factory(FakeSession(user=_USER, count=0)),
        _make_settings(),
        FakeClock(dt.datetime.now()),
        send,
    )
    send.assert_not_called()


@pytest.mark.asyncio
@patch("umai.scheduler.jobs.claim", new_callable=AsyncMock, return_value=True)
@_totals_500
@_wtarget
async def test_first_reminder_sends(mock_wt, mock_dt, mock_claim):
    send = AsyncMock()
    await water_reminder(
        _session_factory(FakeSession(user=_USER, count=0)),
        _make_settings(),
        FakeClock(dt.datetime.now()),
        send,
    )
    send.assert_called_once()
    msg = send.call_args[0][0]
    assert "500" in msg
    assert "2000" in msg
    assert _WATER_REMINDER_MESSAGES[0].split("{")[0] in msg


@pytest.mark.asyncio
@_totals_500
@_wtarget
async def test_second_reminder_too_soon(mock_wt, mock_dt):
    """Second reminder skipped if less than 6h since the first."""
    now = dt.datetime(2026, 8, 23, 15, 0, tzinfo=dt.UTC)
    last_ran = dt.datetime(2026, 8, 23, 13, 0, tzinfo=dt.UTC)
    session = FakeSession(user=_USER, count=1)
    send = AsyncMock()
    patch_path = "umai.scheduler.jobs._water_reminder_last_ran"
    with patch(patch_path, new_callable=AsyncMock, return_value=last_ran):
        await water_reminder(
            _session_factory(session),
            _make_settings(),
            FakeClock(now),
            send,
        )
    send.assert_not_called()


@pytest.mark.asyncio
@patch("umai.scheduler.jobs.claim", new_callable=AsyncMock, return_value=True)
@_totals_500
@_wtarget
async def test_second_reminder_sends_after_delay(mock_wt, mock_dt, mock_claim):
    now = dt.datetime(2026, 8, 23, 19, 30, tzinfo=dt.UTC)
    last_ran = dt.datetime(2026, 8, 23, 13, 0, tzinfo=dt.UTC)
    session = FakeSession(user=_USER, count=1)
    send = AsyncMock()
    patch_path = "umai.scheduler.jobs._water_reminder_last_ran"
    with patch(patch_path, new_callable=AsyncMock, return_value=last_ran):
        await water_reminder(
            _session_factory(session),
            _make_settings(),
            FakeClock(now),
            send,
        )
    send.assert_called_once()
    msg = send.call_args[0][0]
    assert _WATER_REMINDER_MESSAGES[1].split("{")[0] in msg


@pytest.mark.asyncio
@_totals_500
@_wtarget
async def test_third_reminder_too_soon(mock_wt, mock_dt):
    """Third reminder skipped if less than 12h since the second."""
    now = dt.datetime(2026, 8, 24, 1, 0, tzinfo=dt.UTC)
    last_ran = dt.datetime(2026, 8, 23, 19, 30, tzinfo=dt.UTC)
    session = FakeSession(user=_USER, count=2)
    send = AsyncMock()
    patch_path = "umai.scheduler.jobs._water_reminder_last_ran"
    with patch(patch_path, new_callable=AsyncMock, return_value=last_ran):
        await water_reminder(
            _session_factory(session),
            _make_settings(),
            FakeClock(now),
            send,
        )
    send.assert_not_called()


@pytest.mark.asyncio
@patch("umai.scheduler.jobs.claim", new_callable=AsyncMock, return_value=True)
@_totals_500
@_wtarget
async def test_third_reminder_sends_after_delay(mock_wt, mock_dt, mock_claim):
    now = dt.datetime(2026, 8, 24, 8, 0, tzinfo=dt.UTC)
    last_ran = dt.datetime(2026, 8, 23, 19, 30, tzinfo=dt.UTC)
    session = FakeSession(user=_USER, count=2)
    send = AsyncMock()
    patch_path = "umai.scheduler.jobs._water_reminder_last_ran"
    with patch(patch_path, new_callable=AsyncMock, return_value=last_ran):
        await water_reminder(
            _session_factory(session),
            _make_settings(),
            FakeClock(now),
            send,
        )
    send.assert_called_once()
    msg = send.call_args[0][0]
    assert _WATER_REMINDER_MESSAGES[2].split("{")[0] in msg


@pytest.mark.asyncio
@_totals_500
@_wtarget
async def test_fourth_attempt_silenced(mock_wt, mock_dt):
    """After 3 reminders, no more are sent regardless of time."""
    now = dt.datetime(2026, 8, 24, 22, 0, tzinfo=dt.UTC)
    session = FakeSession(user=_USER, count=3)
    send = AsyncMock()
    await water_reminder(
        _session_factory(session),
        _make_settings(),
        FakeClock(now),
        send,
    )
    send.assert_not_called()


@pytest.mark.asyncio
@patch("umai.scheduler.jobs.claim", new_callable=AsyncMock, return_value=False)
@_totals_500
@_wtarget
async def test_claim_failure_silences(mock_wt, mock_dt, mock_claim):
    send = AsyncMock()
    await water_reminder(
        _session_factory(FakeSession(user=_USER, count=0)),
        _make_settings(),
        FakeClock(dt.datetime.now()),
        send,
    )
    send.assert_not_called()
