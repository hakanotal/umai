"""Exponentially weighted moving average over raw scale readings.

All coaching, all goal evaluation and all calibration use the trend, never a raw
reading. A single morning weight is mostly water, glycogen and gut contents; the
trend is the part that corresponds to body composition.

Turicchi et al. compared methods on real smart-scale data and found Kalman
smoothing and EWMA the best performers, RMSE 0.62-0.64% (papers D01). EWMA is
chosen here because it is one line, has one parameter, and updates online.

The same paper found that imputing missing weights makes variability estimates
worse. So gaps are left as gaps: the series carries the days you weighed, and
the trend is carried forward without inventing a reading for the days you did not.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

# A 10-day half-life. Slow enough to ignore a salty dinner, fast enough that a
# real change shows up inside a week.
DEFAULT_HALF_LIFE_DAYS = 10.0

KCAL_PER_KG = 7700.0
"""Energy in a kilogram of body mass change.

An approximation, and not the point. The point is that it is a *constant*
approximation, so its inaccuracy is absorbed into the fitted parameters rather
than propagating.
"""


def alpha_for(half_life_days: float, gap_days: float = 1.0) -> float:
    """EWMA smoothing factor for a gap of `gap_days` since the last reading.

    Gap-aware on purpose. A reading after a two-week silence should not be
    weighted as though it followed yesterday's, and a fixed alpha would do
    exactly that.
    """
    if half_life_days <= 0:
        raise ValueError("half-life must be positive")
    return 1.0 - math.exp(-math.log(2.0) * gap_days / half_life_days)


@dataclass(frozen=True, slots=True)
class TrendPoint:
    date: dt.date
    raw_kg: float | None
    ewma_kg: float


def ewma(
    readings: list[tuple[dt.date, float]],
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> list[TrendPoint]:
    """Smooth a weight series.

    `readings` is (date, kg), in any order, one per day at most. Days without a
    reading produce no point: the caller carries the last trend forward when it
    needs a value for a day, which keeps "no data" distinguishable from "no
    change".
    """
    if not readings:
        return []

    ordered = sorted(readings, key=lambda r: r[0])
    out: list[TrendPoint] = []

    first_date, first_kg = ordered[0]
    trend = first_kg
    out.append(TrendPoint(first_date, first_kg, trend))

    previous = first_date
    for day, kg in ordered[1:]:
        gap = max((day - previous).days, 1)
        a = alpha_for(half_life_days, gap)
        trend = a * kg + (1 - a) * trend
        out.append(TrendPoint(day, kg, trend))
        previous = day

    return out


def trend_on(points: list[TrendPoint], day: dt.date) -> float | None:
    """The trend as it stood on `day`, carrying the last known value forward.

    Returns None before the series starts rather than extrapolating backwards.
    """
    value: float | None = None
    for p in points:
        if p.date > day:
            break
        value = p.ewma_kg
    return value
