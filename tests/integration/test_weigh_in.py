"""One live weigh-in per local day, and a second one corrects the first.

Body weight is a measurement rather than an event: there is one true answer for
a given morning, and a second reading the same day is almost always somebody
fixing a typo. Before this, both were kept, and the live database showed the
consequence — three weigh-ins on each of two consecutive days, none superseded.

Two things go wrong when duplicates accumulate, and both are tested here. The
"↓ 0.3 vs last" line under a weigh-in compared against the reading being
corrected rather than against yesterday. And the trend is an EWMA that
calibration fits against, so a duplicated day carries double weight in the fit
while a mistyped one biases it permanently.

The correction is a *supersede*, never an UPDATE: entries are immutable, the
old row stays and gains a `superseded_by`, and a `corrections` row records what
changed. That is the same mechanism a gram fix uses.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from umai.clock import FakeClock
from umai.core import tools
from umai.db.models import Correction, EntryKind, EntrySource, LogEntry

ZONE = "Europe/Istanbul"


def _at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A UTC instant that reads as `hour:minute` local time in ZONE."""
    from zoneinfo import ZoneInfo

    return dt.datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(ZONE)).astimezone(dt.UTC)


async def _live(session, user) -> list[float]:
    rows = (
        (
            await session.execute(
                select(LogEntry.value)
                .where(
                    LogEntry.user_id == user.id,
                    LogEntry.kind == EntryKind.weight,
                    LogEntry.superseded_by.is_(None),
                )
                .order_by(LogEntry.occurred_at)
            )
        )
        .scalars()
        .all()
    )
    return [float(v) for v in rows]


async def _all_rows(session, user) -> int:
    return int(
        (
            await session.execute(
                select(func.count(LogEntry.id)).where(
                    LogEntry.user_id == user.id, LogEntry.kind == EntryKind.weight
                )
            )
        ).scalar_one()
    )


async def test_a_second_weigh_in_the_same_day_replaces_the_first(session, user):
    await tools.log_weight(session, user, kg=89.9, occurred_at=_at(23, 7))
    entry, replaced = await tools.log_weight(session, user, kg=88.9, occurred_at=_at(23, 7, 30))

    assert replaced == pytest.approx(89.9)
    assert await _live(session, user) == [pytest.approx(88.9)]
    assert entry.value == pytest.approx(88.9)


async def test_the_replaced_row_is_superseded_not_deleted(session, user):
    """Entries are immutable. The audit trail is what keeps "why is my target
    this number" answerable months later."""
    first, _ = await tools.log_weight(session, user, kg=89.9, occurred_at=_at(23, 7))
    second, _ = await tools.log_weight(session, user, kg=88.9, occurred_at=_at(23, 8))

    await session.refresh(first)
    assert first.superseded_by == second.id
    assert await _all_rows(session, user) == 2  # both rows still there


async def test_the_correction_is_recorded(session, user):
    await tools.log_weight(session, user, kg=89.9, occurred_at=_at(23, 7))
    await tools.log_weight(session, user, kg=88.9, occurred_at=_at(23, 8))

    row = (
        await session.execute(select(Correction).where(Correction.field == "weight_kg"))
    ).scalar_one()
    assert row.old_value == "89.9"
    assert row.new_value == "88.9"


async def test_a_weigh_in_on_a_different_day_is_new_data(session, user):
    """The whole point of the per-day rule: corrections replace, days accumulate."""
    await tools.log_weight(session, user, kg=89.9, occurred_at=_at(22, 7))
    _, replaced = await tools.log_weight(session, user, kg=89.4, occurred_at=_at(23, 7))

    assert replaced is None
    assert await _live(session, user) == [pytest.approx(89.9), pytest.approx(89.4)]


async def test_the_day_is_the_users_local_day_not_a_rolling_window(session, user):
    """07:00 and 23:00 on the same Tuesday is one day corrected. 23:00 Tuesday
    and 07:00 Wednesday is two days of data, eight hours apart."""
    await tools.log_weight(session, user, kg=90.0, occurred_at=_at(23, 7))
    _, same_day = await tools.log_weight(session, user, kg=89.5, occurred_at=_at(23, 23))
    assert same_day == pytest.approx(90.0)

    _, next_day = await tools.log_weight(session, user, kg=89.3, occurred_at=_at(24, 7))
    assert next_day is None
    assert await _live(session, user) == [pytest.approx(89.5), pytest.approx(89.3)]


async def test_midnight_belongs_to_the_day_beginning(session, user):
    """`day_bounds` is half-open, so a weigh-in at exactly local midnight starts
    the new day rather than correcting the old one."""
    await tools.log_weight(session, user, kg=90.0, occurred_at=_at(23, 23, 59))
    _, replaced = await tools.log_weight(session, user, kg=89.0, occurred_at=_at(24, 0, 0))
    assert replaced is None


async def test_two_users_do_not_correct_each_other(session, user, other_user):
    await tools.log_weight(session, user, kg=90.0, occurred_at=_at(23, 7))
    _, replaced = await tools.log_weight(session, other_user, kg=62.0, occurred_at=_at(23, 7))

    assert replaced is None
    assert await _live(session, user) == [pytest.approx(90.0)]
    assert await _live(session, other_user) == [pytest.approx(62.0)]


async def test_previous_weight_skips_a_correction(session, user):
    """The bug a user would actually notice.

    `previous_weight` feeds the "↓ 0.3 vs last" line. With the corrected row
    still live it compared against the typo the user had replaced seconds
    earlier, so a fix reported a delta of roughly zero against itself.
    """
    await tools.log_weight(session, user, kg=90.0, occurred_at=_at(22, 7))
    await tools.log_weight(session, user, kg=85.0, occurred_at=_at(23, 7))  # typo: 85 for 89.5
    await tools.log_weight(session, user, kg=89.5, occurred_at=_at(23, 7, 30))  # the fix

    assert await tools.latest_weight(session, user.id) == pytest.approx(89.5)
    assert await tools.previous_weight(session, user.id) == pytest.approx(90.0)


async def test_an_implausible_weight_never_reaches_the_database(session, user):
    await tools.log_weight(session, user, kg=89.0, occurred_at=_at(23, 7))
    with pytest.raises(ValueError, match="plausible"):
        await tools.log_weight(session, user, kg=900.0, occurred_at=_at(23, 8))
    assert await _live(session, user) == [pytest.approx(89.0)]


async def test_re_entering_the_same_number_is_not_treated_as_new_data(session, user):
    """Somebody tapping twice, or unsure the first one registered."""
    await tools.log_weight(session, user, kg=89.0, occurred_at=_at(23, 7))
    _, replaced = await tools.log_weight(session, user, kg=89.0, occurred_at=_at(23, 8))
    assert replaced == pytest.approx(89.0)
    assert await _live(session, user) == [pytest.approx(89.0)]


async def test_the_reply_says_a_correction_happened(session, user):
    """Silently discarding the earlier number would leave somebody who fixed a
    typo unsure whether the fix took."""
    from umai.clock import FakeClock as FC
    from umai.core import agent

    await tools.log_weight(session, user, kg=89.9, occurred_at=_at(23, 7))
    reply = await agent._log_weight(session, user, FC(_at(23, 8)), 88.9)
    assert "89.9" in reply and "88.9" in reply

    fresh = await agent._log_weight(session, user, FC(_at(24, 7)), 88.5)
    assert "vs last" in fresh


async def test_weight_is_not_in_the_edit_list_by_design(session, user):
    """`today_entries` shows food, drink and water — the things a mis-tap makes
    wrong in a way you have to hunt for. A weigh-in is corrected by sending the
    right number, which is why it does not need a row in that list."""
    clock = FakeClock(_at(23, 9))
    await tools.log_weight(session, user, kg=89.9, occurred_at=_at(23, 7))
    assert [e for e in await tools.today_entries(session, user, clock) if e.kind == "weight"] == []


async def test_onboarding_weight_uses_the_same_path(session, onboarding_user):
    """A person who restarts onboarding on the same day corrects their reading
    rather than planting a second one."""
    onboarding_user.tz = ZONE
    await session.flush()
    await tools.log_weight(
        session, onboarding_user, kg=80.0, occurred_at=_at(23, 7), source=EntrySource.manual
    )
    _, replaced = await tools.log_weight(
        session, onboarding_user, kg=81.0, occurred_at=_at(23, 8), source=EntrySource.manual
    )
    assert replaced == pytest.approx(80.0)
