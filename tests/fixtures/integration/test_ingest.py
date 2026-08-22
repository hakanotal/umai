"""Health ingest against real Postgres.

Idempotency and backfill tolerance are the two properties the endpoint must have
from the first commit, and neither can be tested without a real database: the
guarantee lives in a unique constraint and an ON CONFLICT clause.

Duplicate steps corrupt the calibration fit, which is the feature the whole
product rests on, so this is a high-priority test rather than a thorough one.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from umai.db.models import HealthMetric
from umai.ingest.health import extract, ingest

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
        select(HealthMetric.value).where(HealthMetric.metric == "weight_kg")
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
    stored = await session.scalar(select(HealthMetric.recorded_at))
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
