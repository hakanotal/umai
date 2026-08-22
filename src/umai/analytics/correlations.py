"""Statistical relationships, computed in code and thresholded before anything
is narrated.

The model never finds a pattern. It is handed one that survived every test here
and asked to phrase it. That separation is what stops "Umai told me something
true I did not know" from becoming "Umai confidently invented something".

Four gates, all of which must pass:

  sample size    below ~20 paired observations nothing is reportable, however
                 strong it looks
  effect size    a correlation that explains 5% of the variance is not worth a
                 sentence even at p < 0.05
  significance   with a Holm correction, because testing a dozen pairings and
                 reporting the best one is how noise becomes an insight
  plausibility   only pre-declared pairings are tested at all, so the engine
                 cannot dredge

If it finds a pattern in noise it will find patterns in you, and that is the
failure mode that destroys trust in a health assistant faster than any bug.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

MIN_OBSERVATIONS = 20
MIN_ABS_R = 0.30  # ~9% of variance. Below this, not worth a sentence.
ALPHA = 0.05


@dataclass(frozen=True, slots=True)
class Series:
    name: str
    values: dict[dt.date, float]


@dataclass(frozen=True, slots=True)
class Finding:
    x: str
    y: str
    r: float
    n: int
    p_value: float
    lag_days: int
    statement: str

    @property
    def variance_explained(self) -> float:
        return self.r * self.r


# Only these pairings are ever tested. A dredge over every available column
# would produce a significant result by construction.
DECLARED_PAIRINGS: tuple[tuple[str, str, int], ...] = (
    ("sleep_minutes", "kcal_reported", 0),
    ("sleep_minutes", "kcal_reported", 1),  # last night's sleep, today's intake
    ("steps", "kcal_reported", 0),
    ("protein_g", "satiety", 0),
    ("kcal_reported", "weight_change", 1),
    ("water_ml", "satiety", 0),
    ("steps", "sleep_minutes", 0),
)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def p_value_for(r: float, n: int) -> float:
    """Two-tailed p for a correlation, via the t distribution.

    Uses a normal approximation to the t tail, which is close enough at n >= 20
    and avoids a scipy dependency for one number.
    """
    if n < 3 or abs(r) >= 1.0:
        return 0.0
    t = abs(r) * math.sqrt((n - 2) / (1 - r * r))
    # Normal approximation to the two-tailed t tail.
    return 2.0 * (1.0 - _normal_cdf(t))


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _paired(x: Series, y: Series, lag_days: int) -> tuple[list[float], list[float]]:
    """Align two series on date, optionally lagging y behind x.

    Only days present in both contribute. Gaps stay gaps: imputing here would
    manufacture the correlation being tested for.
    """
    xs: list[float] = []
    ys: list[float] = []
    for day, xv in sorted(x.values.items()):
        yv = y.values.get(day + dt.timedelta(days=lag_days))
        if yv is not None:
            xs.append(xv)
            ys.append(yv)
    return xs, ys


def analyse(
    series: dict[str, Series],
    *,
    pairings: tuple[tuple[str, str, int], ...] = DECLARED_PAIRINGS,
    min_observations: int = MIN_OBSERVATIONS,
    min_abs_r: float = MIN_ABS_R,
    alpha: float = ALPHA,
) -> list[Finding]:
    """Every relationship that survives all four gates. Usually none."""
    candidates: list[Finding] = []

    for x_name, y_name, lag in pairings:
        x, y = series.get(x_name), series.get(y_name)
        if x is None or y is None:
            continue

        xs, ys = _paired(x, y, lag)
        if len(xs) < min_observations:
            continue

        r = pearson(xs, ys)
        if r is None or abs(r) < min_abs_r:
            continue

        candidates.append(
            Finding(
                x=x_name,
                y=y_name,
                r=r,
                n=len(xs),
                p_value=p_value_for(r, len(xs)),
                lag_days=lag,
                statement=_describe(x_name, y_name, r, lag, len(xs)),
            )
        )

    return _holm(candidates, alpha)


def _holm(candidates: list[Finding], alpha: float) -> list[Finding]:
    """Holm-Bonferroni. Testing seven pairings and reporting the best one
    without a correction is how a coin flip becomes a health insight."""
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda f: f.p_value)
    m = len(ordered)
    kept: list[Finding] = []
    for i, finding in enumerate(ordered):
        if finding.p_value <= alpha / (m - i):
            kept.append(finding)
        else:
            break  # Holm stops at the first failure
    return kept


_LABELS = {
    "sleep_minutes": "sleep",
    "kcal_reported": "what you log",
    "steps": "steps",
    "protein_g": "protein",
    "satiety": "how full you felt",
    "water_ml": "water",
    "weight_change": "weight change",
}


def _describe(x: str, y: str, r: float, lag: int, n: int) -> str:
    """A plain statement of the association, with its own caveat attached.

    Correlational language throughout, deliberately. The coach may rephrase this
    but may not upgrade it to a cause.
    """
    direction = "more" if r > 0 else "less"
    when = " the next day" if lag else ""
    return (
        f"On days with more {_LABELS.get(x, x)}, you tend to have {direction} "
        f"{_LABELS.get(y, y)}{when} (r={r:.2f}, n={n}). This is an association in "
        f"your own data, not a cause."
    )
