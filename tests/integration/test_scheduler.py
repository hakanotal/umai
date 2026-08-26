"""The per-user tick, against a real database.

`scheduler/jobs.py` had no tests at all before multi-user, which is how it kept
a `select(User).limit(1)` and a globally-keyed claim: with one user in the
database both are indistinguishable from correct. Everything here needs two.

The claim is the load-bearing part. It is what makes a restarted process, a
coalesced misfire and an overlapping tick all safe, and it now has to be per
user — keyed on (job, day) alone, the first person summarised would take the
day and nobody else would hear anything.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from umai.clock import FakeClock
from umai.config.settings import Settings
from umai.core import tools
from umai.db.models import EntryKind, EntrySource, HealthMetric, JobRun, UserStatus
from umai.scheduler import jobs

SETTINGS = Settings(_env_file=None, TZ="Europe/Istanbul", UMAI_WATER_TARGET_ML=2500.0)


def _at(zone: str, day: int, hour: int, minute: int = 0) -> dt.datetime:
    """A UTC instant that reads as `hour:minute` local time in `zone`."""
    local = dt.datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(zone))
    return local.astimezone(dt.UTC)


class Recorder:
    """Stands in for the Telegram send hooks, which take a recipient now."""

    def __init__(self) -> None:
        self.photos: list[tuple[int, str]] = []
        self.texts: list[tuple[int, str]] = []

    async def send(self, telegram_id: int, png: bytes, caption: str) -> None:
        self.photos.append((telegram_id, caption))

    async def send_text(self, telegram_id: int, text: str) -> None:
        self.texts.append((telegram_id, text))

    @property
    def summarised(self) -> set[int]:
        return {tid for tid, _ in self.photos}


def _factory(session):
    """A session factory that always yields the test's own session.

    The test runs inside one transaction that is rolled back, so handing out
    the same session is what keeps the job's writes visible to the assertions —
    and `user_tick` opening a scope per user is preserved in shape, which is
    what matters, because the real one is what stops one user's failure taking
    the others down.
    """

    class _Scope:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return lambda: _Scope()


async def _log_a_meal(session, user, clock) -> None:
    """Something for the summary to be about. An empty day is deliberately
    silent, so without this every assertion below would pass vacuously."""
    await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.water,
        value=250.0,
        unit="ml",
        occurred_at=clock.now(),
        source=EntrySource.manual,
    )


# ---------------------------------------------------------------------------
# Who is due
# ---------------------------------------------------------------------------


async def test_only_active_users_are_ticked(session, user, build_user):
    """A pending or onboarding user has no zone and no profile. Including them
    would mean computing a local day for somebody who has not said where they
    live."""
    session.add(build_user(status=UserStatus.pending, tz=None))
    session.add(build_user(status=UserStatus.onboarding, tz=None))
    session.add(build_user(status=UserStatus.blocked))
    await session.flush()

    found = await jobs.active_users(session)
    assert user.id in {u.id for u in found}
    assert all(u.status == UserStatus.active for u in found)


async def test_due_is_computed_in_each_users_own_zone(session, user, other_user):
    """The whole reason the tick replaced a cron.

    At 21:20 UTC it is already 00:20 on the 24th in Istanbul but still 22:20 on
    the 23rd in London, so the Istanbul user is ten minutes past a 00:10 digest
    and the London one is twenty-two hours short of theirs. A single cron, in
    whatever zone the process happened to be in, could not express that at all.

    It is also the rollover: the Istanbul user's tick runs on the 24th and owes
    the 23rd. A boolean "is it due" would have handed that tick a day ten
    minutes old.
    """
    assert user.tz == "Europe/Istanbul"
    assert other_user.tz == "Europe/London"
    clock = FakeClock(dt.datetime(2026, 8, 23, 21, 20, tzinfo=dt.UTC))

    assert jobs.summary_due_day(user, clock) == dt.date(2026, 8, 23)
    assert jobs.summary_due_day(other_user, clock) is None


async def test_a_users_own_summary_hour_is_respected(session, user):
    """The hour is a column, not a module constant, so two people in the same
    city can want their evening at different times."""
    user.summary_hour, user.summary_minute = 19, 0
    await session.flush()
    clock = FakeClock(_at(user.tz, 23, 19, 5))
    assert jobs.summary_due_day(user, clock) == dt.date(2026, 8, 23)

    user.summary_hour = 22
    await session.flush()
    assert jobs.summary_due_day(user, clock) is None


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


async def test_two_users_each_get_their_own_summary(session, user, other_user):
    """The bug the per-user claim exists to fix. With the claim keyed on
    (job, day) alone, whichever of these two was reached first would take the
    day and the other would be silently skipped."""
    # 23:30 UTC is 02:30 on the 24th in Istanbul and 00:30 on the 24th in
    # London: both are inside the three-hour grace after their own 00:10, and
    # both owe the 23rd. Two hours apart is the widest the pair can be and
    # still be due together, which is the point — one clock cannot serve both.
    evening = FakeClock(dt.datetime(2026, 8, 23, 18, 0, tzinfo=dt.UTC))
    await _log_a_meal(session, user, evening)
    await _log_a_meal(session, other_user, evening)

    clock = FakeClock(dt.datetime(2026, 8, 23, 23, 30, tzinfo=dt.UTC))
    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send)

    assert recorder.summarised == {user.telegram_id, other_user.telegram_id}


async def test_a_second_tick_the_same_day_sends_nothing(session, user):
    """Coalesced misfires and restarts both re-run the tick. Sending the
    digest twice is exactly the thing that makes a bot feel broken."""
    await _log_a_meal(session, user, FakeClock(_at(user.tz, 23, 21, 0)))
    clock = FakeClock(_at(user.tz, 24, 0, 15))

    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send)
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send)

    assert len(recorder.photos) == 1


async def test_an_empty_day_does_not_burn_the_claim(session, user):
    """The ordering fix.

    The claim used to be taken before the emptiness check, so a tick that then
    decided it had nothing to say still spent the day — and somebody who logged
    their first meal at ten in the evening got no summary at all.

    A digest at 00:10 cannot be outrun by a late dinner any more, but the
    invariant still earns its keep: the three-hour grace means several ticks
    look at the same day, and anything that arrives for it in between — a
    backfilled entry, an enrichment that finished after midnight — must still
    find the claim unspent.
    """
    clock = FakeClock(_at(user.tz, 24, 0, 15))
    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send)
    assert recorder.photos == []

    claims = (
        (await session.execute(select(JobRun).where(JobRun.job == "evening_summary")))
        .scalars()
        .all()
    )
    assert claims == []

    # Now something lands for that day after all, and a later tick still
    # finds them rather than standing down on a claim it never earned.
    await _log_a_meal(session, user, FakeClock(_at(user.tz, 23, 21, 0)))
    later = FakeClock(_at(user.tz, 24, 1, 30))
    await jobs.user_tick(_factory(session), SETTINGS, later, recorder.send)
    assert len(recorder.photos) == 1


async def test_nothing_is_sent_before_the_users_summary_time(session, user):
    clock = FakeClock(_at(user.tz, 23, 12, 0))
    await _log_a_meal(session, user, clock)

    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send)
    assert recorder.photos == []


async def test_one_users_failure_does_not_silence_the_others(
    session, user, other_user, monkeypatch
):
    """Each user gets their own session scope for exactly this reason. One
    transaction for the whole pass means the first exception silences
    everybody."""
    evening = FakeClock(dt.datetime(2026, 8, 23, 18, 0, tzinfo=dt.UTC))
    await _log_a_meal(session, user, evening)
    await _log_a_meal(session, other_user, evening)
    clock = FakeClock(dt.datetime(2026, 8, 23, 23, 30, tzinfo=dt.UTC))  # both due

    real = jobs.tools.day_totals
    doomed = user.id

    async def explode(sess, u, *a, **kw):
        if u.id == doomed:
            raise RuntimeError("this user's day is cursed")
        return await real(sess, u, *a, **kw)

    monkeypatch.setattr(jobs.tools, "day_totals", explode)

    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send)

    assert recorder.summarised == {other_user.telegram_id}


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


async def test_the_same_day_can_be_claimed_once_per_user(session, user, other_user):
    day = dt.date(2026, 8, 23)
    assert await jobs.claim(session, "evening_summary", day, user.id) is True
    assert await jobs.claim(session, "evening_summary", day, other_user.id) is True
    assert await jobs.claim(session, "evening_summary", day, user.id) is False


async def test_a_global_claim_is_still_exclusive(session):
    """The partial index.

    A nullable column inside a UNIQUE is not restrictive in Postgres — NULL is
    never equal to NULL — so without the partial index two global claims for
    the same day would both insert and a backup would run twice.
    """
    day = dt.date(2026, 8, 23)
    assert await jobs.claim(session, "backup", day) is True
    assert await jobs.claim(session, "backup", day) is False


@pytest.mark.parametrize("hour", [4, 8, 23])
async def test_water_reminders_stay_within_waking_hours(session, user, hour):
    clock = FakeClock(_at(user.tz, 23, hour))
    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send, recorder.send_text)
    assert recorder.texts == []


async def test_two_users_are_reminded_about_water_independently(session, user, other_user):
    """`_water_reminder_count` is scoped to the user. Unscoped, the second
    person would find three reminders already recorded and never hear
    anything."""
    clock = FakeClock(dt.datetime(2026, 8, 23, 13, 0, tzinfo=dt.UTC))
    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send, recorder.send_text)
    assert {tid for tid, _ in recorder.texts} == {user.telegram_id, other_user.telegram_id}


async def test_the_weeks_series_keeps_a_missing_step_day_missing(session, user):
    """`_last_days` feeds the digest chart, and the chart draws a bar for a
    zero and a "?" for a missing reading. Collapsing None to zero here would
    report a day the phone failed to sync as a day spent sitting down — the
    same conflation `tools.daily_steps` exists to prevent, one layer up."""
    clock = FakeClock(_at(user.tz, 23, 21, 45))
    walked = _at(user.tz, 21, 12, 0)
    session.add(
        HealthMetric(user_id=user.id, metric="steps", value=9000, unit="count", recorded_at=walked)
    )
    await session.flush()

    week = await jobs._last_days(session, user, clock, 7)

    assert len(week.days) == len(week.kcal) == len(week.water_ml) == len(week.steps) == 7
    by_day = dict(zip(week.days, week.steps, strict=True))
    assert by_day[dt.date(2026, 8, 21)] == 9000
    assert by_day[dt.date(2026, 8, 22)] is None


# ---------------------------------------------------------------------------
# The midnight boundary. The digest is due at 00:10 and is about yesterday.
# ---------------------------------------------------------------------------


async def test_a_small_hours_digest_reports_the_day_that_ended(session, user):
    """The whole point of moving past midnight, and the trap in doing so.

    At 00:15 the local day is fifteen minutes old and empty. A digest that
    asked for "today" would find nothing and either say so or say nothing;
    either way the day it was meant to report on is gone.
    """
    await _log_a_meal(session, user, FakeClock(_at(user.tz, 23, 21, 0)))
    clock = FakeClock(_at(user.tz, 24, 0, 15))

    assert jobs.summary_due_day(user, clock) == dt.date(2026, 8, 23)

    recorder = Recorder()
    await jobs.user_tick(_factory(session), SETTINGS, clock, recorder.send)
    assert len(recorder.photos) == 1

    # And it is the 23rd that has been spent, not the 24th.
    claimed = (
        (await session.execute(select(JobRun.day).where(JobRun.job == "evening_summary")))
        .scalars()
        .all()
    )
    assert claimed == [dt.date(2026, 8, 23)]


@pytest.mark.parametrize(
    ("day", "hour", "minute", "expected"),
    [
        (23, 23, 55, None),  # five minutes short: the day is not over
        (24, 0, 5, None),  # past midnight but not yet due
        (24, 0, 10, dt.date(2026, 8, 23)),  # the appointment itself
        (24, 2, 0, dt.date(2026, 8, 23)),  # a restart, still inside the grace
        (24, 3, 5, dt.date(2026, 8, 23)),  # the last minute of the grace
        (24, 3, 15, None),  # past it: better no digest than one at breakfast
        (24, 12, 0, None),  # the middle of the next day owes nothing
    ],
)
async def test_the_due_window_is_a_night_not_an_instant(session, user, day, hour, minute, expected):
    """The tick runs every five minutes on an interval anchored to process
    start, so a due time is a lower bound with a bounded grace, never an
    appointment that has to be hit. A 23:59 digest — the obvious way to say
    "end of day" — would have a sixty-second window that four ticks in five
    miss outright."""
    clock = FakeClock(_at(user.tz, day, hour, minute))
    assert jobs.summary_due_day(user, clock) == expected


async def test_an_evening_digest_still_reports_its_own_day(session, user):
    """The small-hours rule is a rule about small hours. Somebody who has set
    19:00 gets a digest about the day they are still in, and the catch-up for
    it may cross midnight without ever meaning yesterday-minus-one."""
    user.summary_hour, user.summary_minute = 19, 0
    await session.flush()

    assert jobs.summary_due_day(user, FakeClock(_at(user.tz, 23, 19, 5))) == dt.date(2026, 8, 23)
    assert jobs.summary_due_day(user, FakeClock(_at(user.tz, 23, 23, 30))) is None
    assert jobs.summary_due_day(user, FakeClock(_at(user.tz, 23, 21, 30))) == dt.date(2026, 8, 23)
