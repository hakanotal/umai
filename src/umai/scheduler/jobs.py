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
"""

from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select
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
        day = today(clock, user.tz)
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
        caption = tools.format_day(user, totals, target, steps=steps)
        await send(png, caption)


async def _last_days(
    session: AsyncSession, user: User, clock: Clock, n: int
) -> tuple[list[dt.date], list[float], list[float]]:
    """Per-day totals for the last n local days, zeros included (for the chart
    a flat zero is honest: nothing was logged)."""
    end = local_date(clock.now(), user.tz)
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


def schedule(scheduler, session_factory, settings, clock: Clock, send, models=None) -> None:
    """Register the Phase 1 jobs on an APScheduler instance.

    The cron trigger carries the user's timezone explicitly. Without it
    APScheduler uses the *process* timezone, which in the container is UTC
    because nothing sets TZ, and "the 21:30 summary" fired at half past midnight
    local. The docstring here used to claim the shift happened; it did not.

    The health endpoint and webhook do not schedule.
    """
    tz = ZoneInfo(settings.tz)
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
        timezone=tz,
        id="evening_summary",
        coalesce=True,  # a missed window while offline sends once, not per miss
        misfire_grace_time=3600,
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
