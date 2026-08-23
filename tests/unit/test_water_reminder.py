"""The water reminder's escalation, without a database.

`tools.day_totals`, `tools.water_target` and `claim` are mocked; what is under
test is the decision, not the storage. The integration side — that two users
get their own reminders and their own claims — lives in
`tests/integration/test_scheduler.py`.

Every instant here is built by `_at`, which names a *local* hour in the user's
zone. That matters more than it looks: the job refuses to send outside waking
hours, so a test built on `datetime.now()` would pass all day and fail
overnight. The times were written as bare UTC datetimes before the per-user
rewrite, and two of them landed at four in the morning Istanbul time.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from umai.scheduler.jobs import _WATER_REMINDER_MESSAGES, water_reminder

ZONE = "Europe/Istanbul"
TELEGRAM_ID = 4242


def _at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A UTC instant that is `hour` local time in ZONE, in August 2026."""
    local = dt.datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(ZONE))
    return local.astimezone(dt.UTC)


def _make_settings(water_target_ml=2500.0):
    return SimpleNamespace(water_target_ml=water_target_ml)


def _make_totals(water_ml=0.0):
    return SimpleNamespace(water_ml=water_ml)


class FakeClock:
    def __init__(self, now: dt.datetime):
        self._now = now

    def now(self):
        return self._now


class FakeSession:
    """Answers exactly one query: how many reminders have gone out today."""

    def __init__(self, count=0):
        self._count = count

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one=lambda: self._count)


# Mirrors the real row: production reads the zone through User.zone, which
# exists because users.tz is nullable until onboarding finishes. A stub
# carrying only .tz would pass while the code under test could not run.
_USER = SimpleNamespace(
    id="00000000-0000-0000-0000-000000000001",
    tz=ZONE,
    zone=ZONE,
)

_wtarget = patch("umai.scheduler.jobs.tools.water_target", return_value=2500.0)
_totals_zero = patch(
    "umai.scheduler.jobs.tools.day_totals",
    new_callable=AsyncMock,
    return_value=_make_totals(water_ml=0),
)
_totals_500 = patch(
    "umai.scheduler.jobs.tools.day_totals",
    new_callable=AsyncMock,
    return_value=_make_totals(water_ml=500),
)
_claim_ok = patch("umai.scheduler.jobs.claim", new_callable=AsyncMock, return_value=True)


async def _run(session, clock, send):
    await water_reminder(session, _make_settings(), clock, send, _USER, TELEGRAM_ID)


def _sent_text(send: AsyncMock) -> str:
    """The message, from a hook now called as (telegram_id, text)."""
    telegram_id, text = send.call_args[0]
    assert telegram_id == TELEGRAM_ID
    return text


@pytest.mark.asyncio
@_totals_zero
@_wtarget
async def test_nothing_is_sent_before_the_waking_hours(mock_wt, mock_dt):
    """Nobody wants a hydration nudge at four in the morning.

    The escalation deltas space reminders out but cannot prevent the *first* of
    the day arriving overnight, when the target is unmet only because the
    person is asleep.
    """
    send = AsyncMock()
    await _run(FakeSession(count=0), FakeClock(_at(23, 4)), send)
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
    await _run(FakeSession(count=0), FakeClock(_at(23, 16)), send)
    send.assert_not_called()


@pytest.mark.asyncio
@_claim_ok
@_totals_500
@_wtarget
async def test_first_reminder_sends(mock_wt, mock_dt, mock_claim):
    send = AsyncMock()
    await _run(FakeSession(count=0), FakeClock(_at(23, 13)), send)
    send.assert_called_once()
    msg = _sent_text(send)
    assert "500" in msg
    assert "2000" in msg
    assert _WATER_REMINDER_MESSAGES[0].split("{")[0] in msg


@pytest.mark.asyncio
@_claim_ok
@_totals_500
@_wtarget
async def test_the_claim_is_made_for_this_user(mock_wt, mock_dt, mock_claim):
    """Per user, per day. Claimed globally, the second person to be reminded
    would find the day already spent and never hear anything."""
    await _run(FakeSession(count=0), FakeClock(_at(23, 13)), AsyncMock())
    _session, job, _day, user_id = mock_claim.call_args[0]
    assert job == "water_reminder_1"
    assert user_id == _USER.id


@pytest.mark.asyncio
@_totals_500
@_wtarget
async def test_second_reminder_too_soon(mock_wt, mock_dt):
    """Skipped when less than three hours have passed since the first."""
    send = AsyncMock()
    with patch(
        "umai.scheduler.jobs._water_reminder_last_ran",
        new_callable=AsyncMock,
        return_value=_at(23, 13),
    ):
        await _run(FakeSession(count=1), FakeClock(_at(23, 15)), send)
    send.assert_not_called()


@pytest.mark.asyncio
@_claim_ok
@_totals_500
@_wtarget
async def test_second_reminder_sends_after_delay(mock_wt, mock_dt, mock_claim):
    send = AsyncMock()
    with patch(
        "umai.scheduler.jobs._water_reminder_last_ran",
        new_callable=AsyncMock,
        return_value=_at(23, 13),
    ):
        await _run(FakeSession(count=1), FakeClock(_at(23, 19)), send)
    send.assert_called_once()
    assert _WATER_REMINDER_MESSAGES[1].split("{")[0] in _sent_text(send)


@pytest.mark.asyncio
@_totals_500
@_wtarget
async def test_third_reminder_too_soon(mock_wt, mock_dt):
    """Skipped when less than six hours have passed since the second."""
    send = AsyncMock()
    with patch(
        "umai.scheduler.jobs._water_reminder_last_ran",
        new_callable=AsyncMock,
        return_value=_at(23, 19),
    ):
        await _run(FakeSession(count=2), FakeClock(_at(23, 22)), send)
    send.assert_not_called()


@pytest.mark.asyncio
@_claim_ok
@_totals_500
@_wtarget
async def test_third_reminder_sends_after_delay(mock_wt, mock_dt, mock_claim):
    send = AsyncMock()
    with patch(
        "umai.scheduler.jobs._water_reminder_last_ran",
        new_callable=AsyncMock,
        return_value=_at(23, 19),
    ):
        await _run(FakeSession(count=2), FakeClock(_at(24, 11)), send)
    send.assert_called_once()
    assert _WATER_REMINDER_MESSAGES[2].split("{")[0] in _sent_text(send)


@pytest.mark.asyncio
@_totals_500
@_wtarget
async def test_fourth_attempt_silenced(mock_wt, mock_dt):
    """Three and then quiet, whatever the clock says."""
    send = AsyncMock()
    await _run(FakeSession(count=3), FakeClock(_at(24, 22)), send)
    send.assert_not_called()


@pytest.mark.asyncio
@patch("umai.scheduler.jobs.claim", new_callable=AsyncMock, return_value=False)
@_totals_500
@_wtarget
async def test_claim_failure_silences(mock_wt, mock_dt, mock_claim):
    """The restart case: another tick already sent this one."""
    send = AsyncMock()
    await _run(FakeSession(count=0), FakeClock(_at(23, 13)), send)
    send.assert_not_called()
