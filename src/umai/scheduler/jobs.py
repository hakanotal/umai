"""Daily summary, water reminders, weekly review, biweekly recalibration, backups.

Every job must be idempotent. A restart mid-job or a missed window will re-run
it, and sending the evening summary twice is exactly the kind of thing that
makes a bot feel broken. Idempotency here is a (job, day, user) unique row in
job_runs: the job claims a user's day before sending, and a restart finds the
claim and does nothing.

**One tick, not one cron per person.** The scheduled work is per user now, and
each user keeps their own zone and their own summary hour, so there is no
single time at which "the evening summary" fires. Two shapes were possible: a
cron job per distinct timezone, or an interval job that runs often and asks who
has just come due. The interval wins on four counts, in order of weight:

  * The process timezone stops mattering at all. An interval trigger has no
    zone, so the whole `scheduling_tz` apparatus — and the seven-hour
    disagreement between the environment and the user row that it was written
    to patch — simply disappears.
  * Nothing needs re-registering when somebody onboards, moves, or changes
    their summary time. A cron-per-zone scheme has to reconcile its job ids
    against the database at runtime, which is the same query this does anyway.
  * DST becomes a non-question.
  * A missed window self-heals, because the claim is per user per day.

The cost is one indexed query every five minutes over a table with a handful of
rows, and a worst-case five minutes of lateness that nobody perceives.

The enrichment sweep is the exception and keeps its own interval job: it is not
per user, it is meant to run many times a day, and it simply must not overlap
itself, so it takes a Postgres advisory lock and the loser does nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from umai.analytics import charts as charts_mod
from umai.clock import Clock, local_date, today
from umai.core import tools
from umai.db.models import JobRun, User, UserStatus

log = logging.getLogger(__name__)

# How often the tick looks for users who have come due. Five minutes is small
# enough that nobody notices the lateness and large enough that an idle
# deployment costs one indexed query per tick.
TICK_MINUTES = 5

# Mirrors the column defaults on `users`. The row is the source of truth; these
# exist so a caller constructing a user in a test does not have to guess.
DEFAULT_SUMMARY_HOUR = 21
DEFAULT_SUMMARY_MINUTE = 30

# How often the enrichment sweep looks for gaps. Frequent enough that a meal
# logged at lunch has its totals filled in long before the evening summary
# quotes them; infrequent enough that an empty queue costs one indexed query.
ENRICHMENT_MINUTES = 20


async def claim(
    session: AsyncSession, job: str, day: dt.date, user_id: uuid.UUID | None = None
) -> bool:
    """Try to claim (job, day, user). False if already claimed — the restart case.

    INSERT ... ON CONFLICT DO NOTHING: two concurrent runs race, one wins, the
    other sees the existing row and stands down.

    The two cases take different arbiters, and that is not a detail. A nullable
    column inside a UNIQUE is not restrictive in Postgres — NULL is never equal
    to NULL — so a global claim cannot be carried by `uq_job_run_day_user` and
    has a partial index of its own. That index cannot be named as a constraint
    either, because it is not one; it has to be described, which is what
    `index_elements` plus `index_where` does.
    """
    values = insert(JobRun).values(job=job, day=day, user_id=user_id)
    upsert = (
        values.on_conflict_do_nothing(constraint="uq_job_run_day_user")
        if user_id is not None
        else values.on_conflict_do_nothing(
            index_elements=["job", "day"],
            index_where=text("user_id IS NULL"),
        )
    )
    stmt = upsert.returning(JobRun.id)
    won = (await session.execute(stmt)).scalar_one_or_none() is not None
    if won:
        await session.commit()
    return won


async def active_users(session: AsyncSession) -> list[User]:
    """Everyone the scheduled jobs might have something to say to.

    Users without a zone are excluded — and by the CHECK on `users` cannot be
    active either, so the predicate is belt and braces rather than a real case.
    """
    rows = (
        await session.execute(
            select(User).where(User.status == UserStatus.active, User.tz.is_not(None))
        )
    ).scalars()
    return list(rows)


def summary_is_due(user: User, clock: Clock) -> bool:
    """Has this user's local clock reached their summary time today.

    Compared in Python rather than with `AT TIME ZONE`. SQL could express it,
    but then the only way to test the boundary would be to move the database's
    clock; in Python a `FakeClock` puts the whole fleet at any instant, which is
    what the project's time abstraction exists for.

    Only a lower bound is checked, with no upper one: the claim is what stops a
    second send, so a tick at 23:55 for a 21:30 summary correctly sends the one
    that a restart caused to be missed.
    """
    local = clock.now().astimezone(ZoneInfo(user.zone))
    return local.time() >= dt.time(user.summary_hour, user.summary_minute)


async def user_tick(session_factory, settings, clock: Clock, send, send_text=None) -> None:
    """One pass over every active user.

    Each user gets their own session scope, so a failure on one — a corrupt
    row, a chart that will not render, a Telegram error — cannot take the rest
    of the fleet down with it. One transaction for the whole pass would mean
    the first exception silences everybody.

    Which of the two jobs actually has anything to do is each job's own
    decision, because they are due at different times: the summary after a
    per-user hour in the evening, the water reminders through the waking day.
    """
    async with session_factory() as session:
        pending = [(u.id, u.telegram_id) for u in await active_users(session)]

    for user_id, telegram_id in pending:
        for job, extra in ((evening_summary, send), (water_reminder, send_text)):
            if extra is None:
                continue
            try:
                async with session_factory() as session:
                    user = await session.get(User, user_id)
                    if user is None:  # pragma: no cover - deleted mid-tick
                        continue
                    await job(session, settings, clock, extra, user, telegram_id)
            except Exception:
                log.exception("%s failed for %s", job.__name__, telegram_id)


async def evening_summary(
    session: AsyncSession,
    settings,
    clock: Clock,
    send,
    user: User,
    telegram_id: int,
) -> None:
    """One user's evening push: a chart and a couple of sentences.

    `send` is an async callable(telegram_id, png, caption) so the job has no
    Telegram dependency and tests can capture instead of sending. The recipient
    is passed in rather than looked up, because the caller already holds the row.

    **The claim comes after the emptiness check, not before.** Claiming first
    burned the day on a tick that then decided it had nothing to say, so
    somebody who logged their first meal at ten in the evening got no summary at
    all — the day was already spent. Claiming last still guarantees a single
    send, because the claim precedes `send`.
    """
    if not summary_is_due(user, clock):
        return

    day = today(clock, user.zone)
    totals = await tools.day_totals(session, user, clock)
    if totals.entry_count == 0 and totals.kcal == 0:
        return  # nothing logged: say nothing, rather than nag an empty day

    if not await claim(session, "evening_summary", day, user.id):
        return

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
    await send(telegram_id, png, caption)


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


# The local hours a reminder may be sent in. The escalation deltas already
# space the reminders out; this is what stops the first of the day arriving at
# 04:00 for someone whose target is simply not met yet because they are asleep.
WATER_REMINDER_HOURS = range(10, 23)


async def water_reminder(
    session: AsyncSession,
    settings,
    clock: Clock,
    send_text,
    user: User,
    telegram_id: int,
) -> None:
    """Nudge one user to drink water, with escalating intervals.

    The first tick of the waking day that finds them below target sends
    reminder #1; the next only goes out once enough time has passed (3 h after
    #1, 6 h after #2, 12 h after #3). After three the job is silent until
    tomorrow.

    `send_text` is an async callable(telegram_id, str) so the job has no
    Telegram dependency.
    """
    local = clock.now().astimezone(ZoneInfo(user.zone))
    if local.hour not in WATER_REMINDER_HOURS:
        return

    day = today(clock, user.zone)
    count = await _water_reminder_count(session, day, user.id)
    if count >= len(_WATER_REMINDER_DELTAS):
        return  # all three sent, done for the day

    totals = await tools.day_totals(session, user, clock)
    target = tools.water_target(user, settings)
    if totals.water_ml >= target:
        return  # already met the target, no reminder needed

    if count > 0:
        last_ran = await _water_reminder_last_ran(session, day, user.id)
        if last_ran is None:  # pragma: no cover - count > 0 implies a row
            return
        if clock.now() - last_ran < _WATER_REMINDER_DELTAS[count - 1]:
            return  # too soon

    job_name = f"water_reminder_{count + 1}"
    if not await claim(session, job_name, day, user.id):
        return  # another tick got there first

    msg = _WATER_REMINDER_MESSAGES[count].format(
        logged=totals.water_ml,
        remaining=target - totals.water_ml,
        target=target,
    )
    await send_text(telegram_id, msg)


async def _water_reminder_count(session: AsyncSession, day: dt.date, user_id: uuid.UUID) -> int:
    """Count water reminders sent to this user on `day`.

    Scoped to the user, like everything else keyed on job_runs now. Unscoped,
    the second person to be reminded would find three reminders already
    recorded for the day and never hear anything.
    """
    stmt = select(func.count(JobRun.id)).where(
        JobRun.job.like("water_reminder_%"),
        JobRun.day == day,
        JobRun.user_id == user_id,
    )
    return int((await session.execute(stmt)).scalar_one())


async def _water_reminder_last_ran(
    session: AsyncSession, day: dt.date, user_id: uuid.UUID
) -> dt.datetime | None:
    """The timestamp of this user's most recent water reminder on `day`."""
    stmt = (
        select(JobRun.ran_at)
        .where(
            JobRun.job.like("water_reminder_%"),
            JobRun.day == day,
            JobRun.user_id == user_id,
        )
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
) -> None:
    """Register the jobs on an APScheduler instance.

    No timezone argument any more, and none needed. The tick is an interval
    trigger, which has no zone at all, and every decision it makes about when
    something is due is computed from the user's own `tz` column. That removes
    the failure this function used to carry a paragraph about: a cron firing in
    the environment's zone while the summary it produced was computed in the
    user's, seven hours apart, with neither side obviously wrong.

    The health endpoint and webhook do not schedule.
    """
    scheduler.add_job(
        user_tick,
        kwargs={
            "session_factory": session_factory,
            "settings": settings,
            "clock": clock,
            "send": send,
            "send_text": send_text,
        },
        trigger="interval",
        minutes=TICK_MINUTES,
        id="user_tick",
        coalesce=True,  # a missed window while offline runs once, not per miss
        # The pass is idempotent through job_runs, but overlapping passes would
        # duplicate the work for no gain.
        max_instances=1,
        misfire_grace_time=TICK_MINUTES * 60,
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
