"""Health ingest against real Postgres.

Idempotency and backfill tolerance are the two properties the endpoint must have
from the first commit, and neither can be tested without a real database: the
guarantee lives in a unique constraint and an ON CONFLICT clause.

Duplicate steps corrupt the calibration fit, which is the feature the whole
product rests on, so this is a high-priority test rather than a thorough one.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select

from umai.clock import FakeClock
from umai.core import tools
from umai.db.models import HealthMetric, User
from umai.ingest.health import extract, ingest

CLOCK = FakeClock(dt.datetime(2026, 8, 22, 12, 0, tzinfo=dt.UTC))

pytestmark = pytest.mark.integration


def payload(*samples: tuple[str, str, float], units: str = "count") -> dict:
    """Build a Health Auto Export shaped payload."""
    by_metric: dict[str, list[dict]] = {}
    for metric, date, qty in samples:
        by_metric.setdefault(metric, []).append({"date": date, "qty": qty})
    return {
        "data": {
            "metrics": [
                {"name": name, "units": units, "data": data} for name, data in by_metric.items()
            ]
        }
    }


async def _count(session, user) -> int:
    return await session.scalar(
        select(func.count()).select_from(HealthMetric).where(HealthMetric.user_id == user.id)
    )


async def test_replaying_the_same_payload_writes_nothing_new(session, user):
    body = payload(
        ("step_count", "2026-08-19 23:59:00 +0300", 8412),
        ("step_count", "2026-08-20 23:59:00 +0300", 10233),
    )

    first = await ingest(session, user.id, body)
    assert (first.written, first.duplicates) == (2, 0)

    second = await ingest(session, user.id, body)
    assert (second.written, second.duplicates) == (0, 2)

    assert await _count(session, user) == 2


async def test_a_corrected_reading_overwrites_rather_than_duplicating(session, user):
    when = "2026-08-20 07:30:00 +0300"
    await ingest(session, user.id, payload(("body_mass", when, 88.4), units="kg"))
    await ingest(session, user.id, payload(("body_mass", when, 88.6), units="kg"))

    assert await _count(session, user) == 1
    value = await session.scalar(
        select(HealthMetric.value).where(
            HealthMetric.user_id == user.id, HealthMetric.metric == "weight_kg"
        )
    )
    assert value == pytest.approx(88.6)


async def test_a_backlog_delivery_lands_on_the_days_it_describes(session, user):
    """Three days offline, then everything at once. Nothing may land on today."""
    body = payload(
        ("step_count", "2026-08-17 23:59:00 +0300", 5000),
        ("step_count", "2026-08-18 23:59:00 +0300", 9000),
        ("step_count", "2026-08-19 23:59:00 +0300", 12000),
    )
    report = await ingest(session, user.id, body)
    assert report.written == 3

    rows = (
        (
            await session.execute(
                select(HealthMetric.recorded_at).where(HealthMetric.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )

    days = sorted(r.astimezone(dt.UTC).date() for r in rows)
    assert days == [dt.date(2026, 8, 17), dt.date(2026, 8, 18), dt.date(2026, 8, 19)]


async def test_a_payload_containing_the_same_reading_twice_does_not_abort(session, user):
    """ON CONFLICT cannot touch one row twice in a statement, so the duplicate
    has to be removed before the insert or the whole delivery fails."""
    when = "2026-08-20 23:59:00 +0300"
    body = payload(("step_count", when, 8000), ("step_count", when, 8100))

    report = await ingest(session, user.id, body)

    assert report.written == 1
    assert await _count(session, user) == 1


async def test_one_bad_sample_does_not_discard_the_delivery(session, user):
    body = payload(
        ("step_count", "2026-08-20 23:59:00 +0300", 9000),
        ("step_count", "not a timestamp", 9000),
        ("body_mass", "2026-08-20 07:00:00 +0300", 900_000),  # unit error
    )
    report = await ingest(session, user.id, body)

    assert report.written == 1
    assert report.rejected == 2
    assert await _count(session, user) == 1


async def test_timestamps_are_stored_as_utc(session, user):
    await ingest(session, user.id, payload(("step_count", "2026-08-20 23:59:00 +0300", 100)))
    # Scoped to this test's own user. A table-wide select passes only against a
    # virgin database and fails the moment the dev DB has ever received a real
    # payload.
    stored = await session.scalar(
        select(HealthMetric.recorded_at).where(HealthMetric.user_id == user.id)
    )
    assert stored.utcoffset() == dt.timedelta(0)
    assert stored.hour == 20  # 23:59 +03:00 is 20:59 UTC


# --- extraction, which needs no database -----------------------------------


def test_naive_timestamps_are_rejected_rather_than_assumed():
    """A naive timestamp guessed as local is wrong twice a year, invisibly."""
    readings, rejects = extract(payload(("step_count", "2026-08-20 23:59:00", 9000)))
    assert readings == []
    assert "naive" in rejects[0]


def test_sleep_phases_are_summed_and_converted():
    body = {
        "data": {
            "metrics": [
                {
                    "name": "sleep_analysis",
                    "units": "hr",
                    "data": [
                        {
                            "date": "2026-08-20 07:00:00 +0300",
                            "deep": 1.2,
                            "core": 4.0,
                            "rem": 1.3,
                            "awake": 0.5,
                        }
                    ],
                }
            ]
        }
    }
    readings, _ = extract(body)
    assert len(readings) == 1
    # 6.5 hours asleep, awake time excluded, converted to minutes.
    assert readings[0].value == pytest.approx(390.0)


# ---------------------------------------------------------------------------
# Reading steps back: the local-day bucketing
# ---------------------------------------------------------------------------
#
# The `user` fixture is Europe/Istanbul (UTC+3, no DST since 2016), so a local
# day runs from 21:00 UTC the previous day to 21:00 UTC. Every boundary case
# below is stated in local time, because that is the only frame in which the
# expected answers are obvious.


async def test_steps_for_a_day_sum_across_samples(session, user):
    await ingest(
        session,
        user.id,
        payload(
            ("step_count", "2026-08-20 09:00:00 +0300", 1200),
            ("step_count", "2026-08-20 13:00:00 +0300", 3400),
            ("step_count", "2026-08-20 19:00:00 +0300", 900),
        ),
    )
    day = await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 20))
    assert day is not None
    assert day.steps == 5500
    assert day.samples == 3
    assert not day.suspect


async def test_samples_bucket_by_local_day_not_utc_day(session, user):
    """The bug this whole timezone convention exists to prevent.

    23:30 Istanbul is 20:30 UTC — the same calendar date in both frames, so a
    naive UTC implementation gets it right by accident. 01:30 Istanbul is 22:30
    UTC on the *previous* date, and that is where a UTC implementation puts the
    steps on the wrong day. Both directions are asserted together so that half
    a correct implementation cannot pass.
    """
    await ingest(
        session,
        user.id,
        payload(
            ("step_count", "2026-08-20 23:30:00 +0300", 700),
            ("step_count", "2026-08-21 01:30:00 +0300", 300),
        ),
    )
    twentieth = await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 20))
    twentyfirst = await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 21))

    assert twentieth is not None and twentieth.steps == 700
    assert twentyfirst is not None and twentyfirst.steps == 300


async def test_a_day_with_no_readings_is_none_not_zero(session, user):
    """None and zero are different answers.

    `calibration.activity_offset_kcal` reads None as "assume a typical 8,000
    step day" and a real zero as roughly -350 kcal. A day the phone failed to
    sync, reported as zero, would tell the engine the user was bedbound.
    """
    await ingest(session, user.id, payload(("step_count", "2026-08-20 09:00:00 +0300", 5000)))

    assert await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 19)) is None

    await ingest(session, user.id, payload(("step_count", "2026-08-19 09:00:00 +0300", 0)))
    sedentary = await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 19))
    assert sedentary is not None
    assert sedentary.steps == 0


async def test_backfill_of_three_days_buckets_per_day(session, user):
    """The phone delivering three days at once is three days, not one."""
    await ingest(
        session,
        user.id,
        payload(
            ("step_count", "2026-08-18 10:00:00 +0300", 1000),
            ("step_count", "2026-08-19 10:00:00 +0300", 2000),
            ("step_count", "2026-08-20 10:00:00 +0300", 3000),
        ),
    )
    by_day = await tools.steps_by_day(session, user, dt.date(2026, 8, 18), dt.date(2026, 8, 20))
    assert {d: v.steps for d, v in by_day.items()} == {
        dt.date(2026, 8, 18): 1000,
        dt.date(2026, 8, 19): 2000,
        dt.date(2026, 8, 20): 3000,
    }


async def test_replaying_a_payload_does_not_change_the_daily_total(session, user):
    """Idempotency stated at the level the product actually depends on.

    The constraint-level version of this is tested above; this is the one that
    would have caught a natural key that looked right and doubled every total.
    """
    delivery = payload(
        ("step_count", "2026-08-20 09:00:00 +0300", 1200),
        ("step_count", "2026-08-20 13:00:00 +0300", 3400),
    )
    await ingest(session, user.id, delivery)
    first = await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 20))

    report = await ingest(session, user.id, delivery)
    second = await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 20))

    assert report.written == 0
    assert first is not None and second is not None
    assert first.steps == second.steps == 4600
    assert first.samples == second.samples == 2


async def test_other_metrics_and_other_users_do_not_leak_into_the_total(session, user):
    other = User(
        id=uuid.uuid4(),
        telegram_id=int(uuid.uuid4().int % 1_000_000_000),
        tz="Europe/Istanbul",
    )
    session.add(other)
    await session.flush()

    await ingest(
        session,
        user.id,
        payload(
            ("step_count", "2026-08-20 09:00:00 +0300", 1000),
            ("body_mass", "2026-08-20 08:00:00 +0300", 90),
        ),
    )
    await ingest(session, other.id, payload(("step_count", "2026-08-20 10:00:00 +0300", 9999)))

    mine = await tools.daily_steps(session, user, CLOCK, dt.date(2026, 8, 20))
    assert mine is not None
    assert mine.steps == 1000
    assert mine.samples == 1


async def test_bucketing_follows_the_zone_not_a_fixed_offset(session):
    """DST correctness.

    Istanbul has been UTC+3 with no DST since 2016, so it cannot show this.
    London can: on 2026-03-29 the clocks go forward at 01:00, so that local day
    is 23 hours long and starts at 00:00 UTC, while the day before starts at
    00:00 UTC too but the day after starts at 23:00 UTC. Only a real tz-database
    lookup gets these on the right dates.
    """
    londoner = User(
        id=uuid.uuid4(),
        telegram_id=int(uuid.uuid4().int % 1_000_000_000),
        tz="Europe/London",
    )
    session.add(londoner)
    await session.flush()

    await ingest(
        session,
        londoner.id,
        payload(
            # 00:30 GMT on the 29th, before the jump — the 29th.
            ("step_count", "2026-03-29 00:30:00 +0000", 100),
            # 02:30 BST on the 29th, after the jump (01:30 UTC) — still the 29th.
            ("step_count", "2026-03-29 02:30:00 +0100", 200),
            # 00:30 BST on the 30th (23:30 UTC on the 29th) — the 30th.
            ("step_count", "2026-03-30 00:30:00 +0100", 400),
        ),
    )
    by_day = await tools.steps_by_day(session, londoner, dt.date(2026, 3, 29), dt.date(2026, 3, 30))
    assert {d: v.steps for d, v in by_day.items()} == {
        dt.date(2026, 3, 29): 300,
        dt.date(2026, 3, 30): 400,
    }
