"""Health Auto Export webhook: steps, sleep, weight.

Two properties are required from the first commit, because the phone will
eventually deliver three days of backlog in one request after being offline:

  idempotent          a natural key on (user, metric, timestamp, source) with an
                      upsert, not an insert. Duplicate steps corrupt the
                      calibration fit, and the fit is the feature the product
                      rests on.
  backfill-tolerant   never assume the payload is about today. A record arriving
                      now may describe last Tuesday, and it must land on last
                      Tuesday.

The payload shape below follows Health Auto Export's REST export format. Record
the first real one with `tools/replay_health.py --record` and keep it as a
fixture: the real shape and the documented shape are usually a small but
expensive distance apart.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from umai.db.models import HealthMetric

log = logging.getLogger(__name__)

SOURCE = "health_auto_export"

# Health Auto Export's metric names, mapped to the ones used internally. Anything
# unmapped is stored under its own name rather than dropped: an unexpected metric
# is data, and discarding it silently loses history that cannot be recovered.
METRIC_ALIASES = {
    "step_count": "steps",
    "body_mass": "weight_kg",
    "weight_body_mass": "weight_kg",
    "sleep_analysis": "sleep_minutes",
    "active_energy": "active_kcal",
    "basal_energy_burned": "basal_kcal",
    "dietary_water": "water_ml",
    "resting_heart_rate": "resting_hr",
}

# Values outside these bounds are rejected rather than stored. A 900 kg weight
# reading is a unit error or a sync glitch, and one of them in the series moves
# the trend for a fortnight.
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "steps": (0, 100_000),
    "weight_kg": (20, 400),
    "sleep_minutes": (0, 24 * 60),
    "active_kcal": (0, 10_000),
    "water_ml": (0, 20_000),
    "resting_hr": (20, 200),
}


@dataclass(frozen=True, slots=True)
class IngestReport:
    received: int
    written: int
    duplicates: int
    rejected: int
    rejects: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        return (
            f"{self.received} received, {self.written} written, "
            f"{self.duplicates} already known, {self.rejected} rejected"
        )


@dataclass(frozen=True, slots=True)
class Reading:
    metric: str
    value: float
    unit: str | None
    recorded_at: dt.datetime


def normalise_metric(name: str) -> str:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    return METRIC_ALIASES.get(key, key)


def _parse_timestamp(raw: str) -> dt.datetime | None:
    """Health Auto Export sends offsets like '2026-08-21 09:15:00 +0300'.

    Whatever arrives, the result is aware and converted to UTC. A naive
    timestamp is treated as a rejection rather than assumed to be local: the
    guess is invisible and wrong twice a year.
    """
    stamp = raw.strip()
    candidates = (
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    )
    parsed: dt.datetime | None = None
    for fmt in candidates:
        try:
            parsed = dt.datetime.strptime(stamp, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(stamp)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def extract(payload: dict[str, Any]) -> tuple[list[Reading], list[str]]:
    """Flatten a Health Auto Export payload into readings.

    Returns (readings, rejection reasons). Rejections are reported rather than
    raised: one malformed sample must not discard the other four hundred in the
    same backlog delivery.
    """
    readings: list[Reading] = []
    rejects: list[str] = []

    metrics = (payload.get("data") or {}).get("metrics") or payload.get("metrics") or []
    for metric in metrics:
        name = normalise_metric(str(metric.get("name", "")))
        unit = metric.get("units")
        if not name:
            continue

        for sample in metric.get("data") or []:
            when = _parse_timestamp(str(sample.get("date", "")))
            if when is None:
                rejects.append(f"{name}: unparseable or naive timestamp {sample.get('date')!r}")
                continue

            # Sleep arrives as phases; steps and weight as a plain quantity.
            # _sleep_minutes already returns minutes, so it must not fall through
            # to the hours conversion below.
            value = sample.get("qty")
            already_minutes = False
            if value is None and name == "sleep_minutes":
                value = _sleep_minutes(sample)
                already_minutes = True
            if value is None:
                rejects.append(f"{name}: no quantity at {when.isoformat()}")
                continue

            try:
                value = float(value)
            except (TypeError, ValueError):
                rejects.append(f"{name}: non-numeric value {value!r}")
                continue

            if (
                name == "sleep_minutes"
                and not already_minutes
                and (metric.get("units") or "").lower() in {"hr", "hours"}
            ):
                value *= 60.0

            lo, hi = PLAUSIBLE.get(name, (float("-inf"), float("inf")))
            if not lo <= value <= hi:
                rejects.append(f"{name}: {value} outside the plausible range {lo}-{hi}")
                continue

            readings.append(Reading(metric=name, value=value, unit=unit, recorded_at=when))

    return readings, rejects


def _sleep_minutes(sample: dict[str, Any]) -> float | None:
    """Sum the asleep phases, ignoring time in bed awake."""
    phases = ("deep", "core", "rem", "asleep")
    total = sum(float(sample.get(p) or 0.0) for p in phases)
    return total * 60.0 if total else None


async def ingest(
    session: AsyncSession,
    user_id: Any,
    payload: dict[str, Any],
) -> IngestReport:
    """Upsert a payload. Safe to call twice with the same body."""
    readings, rejects = extract(payload)
    if not readings:
        return IngestReport(0, 0, 0, len(rejects), tuple(rejects[:10]))

    # Deduplicate within the payload itself before it reaches Postgres.
    #
    # ON CONFLICT cannot update the same row twice in one statement, so a
    # backlog delivery containing the same reading twice would abort the whole
    # insert. Last one wins, matching the upsert semantics below.
    deduped: dict[tuple[str, dt.datetime], Reading] = {}
    for r in readings:
        deduped[(r.metric, r.recorded_at)] = r

    rows = [
        {
            "user_id": user_id,
            "metric": r.metric,
            "value": r.value,
            "unit": r.unit,
            "recorded_at": r.recorded_at,
            "source": SOURCE,
        }
        for r in deduped.values()
    ]

    # ON CONFLICT DO UPDATE rather than DO NOTHING: a corrected reading for a
    # timestamp already seen should win, which happens when Apple Health
    # reconciles a manual edit after the fact.
    base = insert(HealthMetric).values(rows)
    upsert = base.on_conflict_do_update(
        constraint="uq_health_natural",
        set_={"value": base.excluded.value, "unit": base.excluded.unit},
    ).returning(HealthMetric.id, text("(xmax = 0) AS inserted"))

    result = await session.execute(upsert)
    touched = result.all()

    # xmax is zero only on a genuine insert, which is how an upsert distinguishes
    # a new reading from one it has already seen. Without this every replay would
    # report itself as fresh data. Accessed by position rather than by name
    # because the asyncpg dialect does not expose a text() column's AS alias as
    # an attribute on the returned Row.
    written = sum(1 for row in touched if row[1])
    duplicates = len(touched) - written

    log.info(
        "health ingest: %d readings, %d written, %d already known, %d rejected",
        len(readings),
        written,
        duplicates,
        len(rejects),
    )
    return IngestReport(
        received=len(readings),
        written=written,
        duplicates=max(duplicates, 0),
        rejected=len(rejects),
        rejects=tuple(rejects[:10]),
    )
