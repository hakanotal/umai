"""Daily summary, evening check-in, weekly review, biweekly recalibration, backups.

Every job must be idempotent. A restart mid-job or a missed window will re-run
it, and sending the evening summary twice is exactly the kind of thing that
makes a bot feel broken. Idempotency here is a (job, date) unique row in
job_runs: the job claims its day before sending, and a restart finds the claim
and does nothing.

Phase 1 runs two jobs: the evening summary at 21:30 local, and the food-table
enrichment sweep every twenty minutes. Check-ins and the weekly review arrive
with Phase 3/4; the scaffolding is deliberately minimal so adding them is a new
function, not a redesign.

The two have opposite idempotency needs, and get different mechanisms. The
summary must happen exactly once per day, so it claims a (job, day) row. The
enrichment sweep is meant to run many times a day and simply must not overlap
itself, so it takes a Postgres advisory lock and the loser does nothing.

Water reminders use (job, day) uniqueness like the summary, but allow up to
three claims per day (water_reminder_1, water_reminder_2, water_reminder_3).
Escalating intervals — 3 h, 6 h, 12 h — are enforced by checking how long
has elapsed since the previous reminder, not by scheduling separate jobs.
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from umai.analytics import charts as charts_mod
from umai.clock import Clock, local_date, today
from umai.core import tools
from umai.db.models import JobRun, User

log = logging.getLogger(__name__)

SUMMARY_HOUR = 21
SUMMARY_MINUTE = 30

# How often the enrichment sweep looks for gaps. Frequent enough that a meal
# logged at lunch has its totals filled in long before the evening summary
# quotes them; infrequent enough that an empty queue costs one indexed query.
ENRICHMENT_MINUTES = 20


async def claim(session: AsyncSession, job: str, day: dt.date) -> bool:
    """Try to claim (job, day). False if already claimed — the restart case.

    INSERT ... ON CONFLICT DO NOTHING: two concurrent runs race, one wins,
    the other sees the existing row and stands down.
    """
    stmt = (
        insert(JobRun)
        .values(job=job, day=day)
        .on_conflict_do_nothing(constraint="uq_job_run_day")
        .returning(JobRun.id)
    )
    won = (await session.execute(stmt)).scalar_one_or_none() is not None
    if won:
        await session.commit()
    return won


async def evening_summary(session_factory, settings, clock: Clock, send) -> None:
    """The 21:30 push: a chart and a couple of sentences, no wall of numbers.

    `send` is an async callable(bytes_png, caption) so the job has no Telegram
    dependency and tests can capture instead of sending.
    """
    async with session_factory() as session:
        user = (await session.execute(select(User).limit(1))).scalar_one_or_none()
        if user is None:
            return
        day = today(clock, user.zone)
        if not await claim(session, "evening_summary", day):
            return

        totals = await tools.day_totals(session, user, clock)
        if totals.entry_count == 0 and totals.kcal == 0:
            return  # nothing logged: say nothing, rather than nag an empty day

        target = None
        weight = await tools.latest_weight(session, user.id)
        if weight is not None:
            try:
                target = tools.current_target(user, weight, clock)
            except RuntimeError:
                target = None

        days, kcal, _protein = await _last_days(session, user, clock, 7)
        png = charts_mod.week_kcal(days, kcal, target.kcal_target if target else None)
        steps = await tools.daily_steps(session, user, clock)
        wt = tools.water_target(user, settings)
        caption = tools.format_day(user, totals, target, steps=steps, water_target_ml=wt)
        await send(png, caption)


async def _last_days(
    session: AsyncSession, user: User, clock: Clock, n: int
) -> tuple[list[dt.date], list[float], list[float]]:
    """Per-day totals for the last n local days, zeros included (for the chart
    a flat zero is honest: nothing was logged)."""
    end = local_date(clock.now(), user.zone)
    days: list[dt.date] = []
    kcal: list[float] = []
    protein: list[float] = []
    for back in range(n - 1, -1, -1):
        day = end - dt.timedelta(days=back)
        totals = await tools.day_totals(session, user, clock, day)
        days.append(day)
        kcal.append(totals.kcal)
        protein.append(totals.protein_g)
    return days, kcal, protein


async def enrichment_sweep(session_factory, models, clock: Clock) -> None:
    """Research the foods the table is missing. See core/enrichment.py.

    Off the critical path by construction: the user's reply never waits on it,
    and a provider outage here shows up as a log line and a retry in twenty
    minutes rather than as a failed food log.
    """
    from umai.core import enrichment

    try:
        report = await enrichment.enrich_once(session_factory, models, clock)
    except Exception:
        log.exception("enrichment sweep failed")
        return
    if report.did_work:
        log.info("enrichment sweep: %s", enrichment.summarise(report))


async def scheduling_tz(session_factory, settings) -> str:
    """The zone the evening summary should fire in.

    The user's own, read from the database, because that is the same column
    every other part of the job already uses: `evening_summary` computes "today"
    with `today(clock, user.zone)` and `_last_days` walks back through
    `local_date(..., user.zone)`. Firing the job on `settings.tz` while its
    contents were computed in `user.zone` meant the two could disagree — and they
    did, by seven hours, with the environment saying one city and the user row
    another.

    Falls back to the configured zone only when no user exists yet, which is the
    first boot before anyone has sent a message.
    """
    async with session_factory() as session:
        tz = (await session.execute(select(User.tz).limit(1))).scalar_one_or_none()
    if tz and tz != settings.tz:
        log.warning(
            "scheduling in the user's zone %s, which differs from TZ=%s; "
            "the user row wins because every daily total is computed from it",
            tz,
            settings.tz,
        )
    return tz or settings.tz


# Water reminder escalation: hours to wait after each reminder before sending
# the next. Three reminders total, then silence for the day.
_WATER_REMINDER_DELTAS = [
    dt.timedelta(hours=3),
    dt.timedelta(hours=6),
    dt.timedelta(hours=12),
]
_WATER_REMINDER_MESSAGES = [
    "💧 Time to drink some water! You've had {logged:.0f} ml today, {remaining:.0f} ml to go.",
    "💧 Still haven't hit your water target — {logged:.0f} ml logged,"
    " {remaining:.0f} ml to go. Drink up!",
    "💧 Last reminder: you're at {logged:.0f} ml out of {target:.0f} ml. Don't forget to hydrate!",
]


async def water_reminder(session_factory, settings, clock: Clock, send_text) -> None:
    """Nudge the user to drink water with escalating intervals.

    Fires every 3 hours during waking hours (10, 13, 16, 19, 22). The first
    tick that finds the user below target sends reminder #1. Subsequent ticks
    only send when enough time has elapsed since the last reminder (6 h after
    #1, 12 h after #2). After 3 reminders the job stays silent for the day.

    `send_text` is an async callable(str) so the job has no Telegram
    dependency.
    """
    async with session_factory() as session:
        user = (await session.execute(select(User).limit(1))).scalar_one_or_none()
        if user is None:
            return
        day = today(clock, user.zone)

        # How many reminders have already been sent today?
        count = await _water_reminder_count(session, day)
        if count >= len(_WATER_REMINDER_DELTAS):
            return  # all three sent, done for the day

        totals = await tools.day_totals(session, user, clock)
        target = tools.water_target(user, settings)

        if totals.water_ml >= target:
            return  # already met the target, no reminder needed

        # Enforce escalating delay: check that enough time has passed since
        # the previous reminder.
        if count > 0:
            last_ran = await _water_reminder_last_ran(session, day)
            if last_ran is None:
                # Shouldn't happen (count > 0 implies a row exists), but be safe.
                return
            elapsed = clock.now() - last_ran
            if elapsed < _WATER_REMINDER_DELTAS[count - 1]:
                return  # too soon

        # Send the reminder.
        job_name = f"water_reminder_{count + 1}"
        if not await claim(session, job_name, day):
            return  # race condition: another instance already sent it

        remaining = target - totals.water_ml
        msg = _WATER_REMINDER_MESSAGES[count].format(
            logged=totals.water_ml,
            remaining=remaining,
            target=target,
        )
        await send_text(msg)


async def _water_reminder_count(session: AsyncSession, day: dt.date) -> int:
    """Count water reminders sent on `day`."""
    stmt = select(func.count(JobRun.id)).where(
        JobRun.job.like("water_reminder_%"),
        JobRun.day == day,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _water_reminder_last_ran(session: AsyncSession, day: dt.date) -> dt.datetime | None:
    """The timestamp of the most recent water reminder on `day`."""
    stmt = (
        select(JobRun.ran_at)
        .where(JobRun.job.like("water_reminder_%"), JobRun.day == day)
        .order_by(JobRun.ran_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def schedule(
    scheduler,
    session_factory,
    settings,
    clock: Clock,
    send,
    send_text=None,
    models=None,
    tz: str | None = None,
) -> None:
    """Register the Phase 1 jobs on an APScheduler instance.

    `tz` is the zone the cron fires in and should come from `scheduling_tz`, so
    that the hour the summary arrives and the day it summarises are the same
    frame. Passing nothing falls back to the configured zone, which is only
    right before a user exists.

    Without an explicit zone APScheduler uses the *process* timezone, which in
    a container is UTC unless something sets TZ, and "the 21:30 summary" fires
    at half past midnight local.

    The health endpoint and webhook do not schedule.
    """
    zone = ZoneInfo(tz or settings.tz)
    scheduler.add_job(
        evening_summary,
        kwargs={
            "session_factory": session_factory,
            "settings": settings,
            "clock": clock,
            "send": send,
        },
        trigger="cron",
        hour=SUMMARY_HOUR,
        minute=SUMMARY_MINUTE,
        timezone=zone,
        id="evening_summary",
        coalesce=True,  # a missed window while offline sends once, not per miss
        misfire_grace_time=3600,
    )

    if send_text is not None:
        scheduler.add_job(
            water_reminder,
            kwargs={
                "session_factory": session_factory,
                "settings": settings,
                "clock": clock,
                "send_text": send_text,
            },
            trigger="cron",
            hour="10,13,16,19,22",
            timezone=zone,
            id="water_reminder",
            coalesce=True,
            misfire_grace_time=1800,
        )

    if models is not None:
        scheduler.add_job(
            enrichment_sweep,
            kwargs={
                "session_factory": session_factory,
                "models": models,
                "clock": clock,
            },
            trigger="interval",
            minutes=ENRICHMENT_MINUTES,
            id="enrichment_sweep",
            coalesce=True,
            # Overlap is already impossible via the advisory lock; this keeps a
            # slow sweep from queueing ticks behind it as well.
            max_instances=1,
            misfire_grace_time=ENRICHMENT_MINUTES * 60,
        )
