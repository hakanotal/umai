"""The time abstraction.

Nothing anywhere in Umai calls the wall clock directly; everything takes a
Clock. This is what makes the time-dependent features (trend weight, the
calibration window, evening check-ins) testable in seconds instead of weeks.

See technical-implementation.md section 8. The ban is enforced by ruff and by
`just check-clock`.
"""

from __future__ import annotations

import datetime as _dt
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

UTC = _dt.UTC


@runtime_checkable
class Clock(Protocol):
    """Everything that needs to know the time takes one of these."""

    def now(self) -> _dt.datetime:
        """Current instant, always timezone-aware and always UTC."""
        ...


class SystemClock:
    """The real clock. Constructed once, at the composition root."""

    def now(self) -> _dt.datetime:
        return _dt.datetime.now(tz=UTC)


class FakeClock:
    """A clock you can push around. Tests and the simulator use this.

    >>> c = FakeClock(_dt.datetime(2026, 1, 1, tzinfo=UTC))
    >>> c.advance(days=14)
    >>> c.now().day
    15
    """

    def __init__(self, start: _dt.datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock needs an aware datetime; naive time is a bug source")
        self._t = start.astimezone(UTC)

    def now(self) -> _dt.datetime:
        return self._t

    def advance(self, **kw: float) -> None:
        self._t += _dt.timedelta(**kw)

    def set(self, t: _dt.datetime) -> None:
        self._t = t.astimezone(UTC)


# ---------------------------------------------------------------------------
# Local-day boundaries
# ---------------------------------------------------------------------------
#
# Storage is UTC without exception. But "did I log dinner today" is a question
# about the *local* day, and a naive datetime anywhere in the middle produces a
# bug that only manifests near midnight and only sometimes. So the conversion
# happens here, in one place, and takes the timezone explicitly.


def local_date(when: _dt.datetime, tz: str) -> _dt.date:
    """The calendar date `when` falls on, for someone living in `tz`."""
    return when.astimezone(ZoneInfo(tz)).date()


def day_bounds(day: _dt.date, tz: str) -> tuple[_dt.datetime, _dt.datetime]:
    """The UTC half-open interval [start, end) covering a local calendar day.

    Half-open on purpose: an entry at exactly midnight belongs to the day
    beginning, never to both.
    """
    zone = ZoneInfo(tz)
    start = _dt.datetime.combine(day, _dt.time.min, tzinfo=zone)
    end = start + _dt.timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def today(clock: Clock, tz: str) -> _dt.date:
    return local_date(clock.now(), tz)
