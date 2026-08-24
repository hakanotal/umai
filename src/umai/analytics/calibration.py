"""The logging bias factor k, fitted from the energy balance identity.

    true_intake - true_expenditure = change in body energy stores

Ravelli and Schoeller put that identity at within 2% (papers C03). It is not an
approximation invented for this project. Section 3 of the plan solves it for the
logging bias factor.

Substituting what is actually observable:

    k * reported_intake - tdee = delta_trend_weight * 7700

Two unknowns, and a single window gives one equation, so a single window cannot
separate them. What separates them is *several* windows in which reported intake
varies: k multiplies intake, expenditure does not, so weeks where you ate more
and weeks where you ate less pull the two apart. That is why this is fitted
across overlapping windows rather than solved week by week.

A FINDING FROM THE SIMULATOR, and the most important thing to know about this
module.

k and expenditure are collinear, and over a few months of consistent eating they
cannot be separated well. The reason is arithmetic rather than fixable: across a
fortnight the sum of reported intake barely varies (day-to-day noise averages
out), while the observed balance carries the scale's noise, which is roughly
0.6 kg or 4600 kcal. Fitting two parameters against that leaves each one poorly
determined, and no amount of extra history fixes it, because the noise does not
shrink relative to the variation that identifies them.

What IS well determined is the combination the product actually uses. Against a
planted truth of k=1.282 and TDEE=2400, a 180-day fit returns k=1.03 and
TDEE=2012 -- both badly wrong -- yet the reported-intake target derived from
them lands within about 1% of correct, because the errors cancel in exactly the
direction the target is computed.

So: `reported_intake_target()` is the supported output of this module, and it is
trustworthy. `k_factor` and `tdee_estimate` are internal parameters. Do not show
either as a fact about the user's metabolism, and do not use one without the
other. The confidence intervals are conditional on the prior and are narrower
than the truth.

Everything here is guarded, in this order:

  coverage gating   a window with too few logged days is discarded outright.
                    Without this the engine learns that you eat 900 kcal a day
                    because you stopped logging dinners.
  regularisation    the fit is pulled toward the priors, so early fits with
                    little data stay near the formula estimate rather than
                    swinging to whatever two weeks of noise implies.
  damping           the state moves a fraction of the way to the new fit, so
                    one bad week cannot swing the model.
  clamping          k is held to 0.7-1.4 and expenditure to a sane range,
                    because outside those the likelier explanation is a broken
                    fit than a remarkable metabolism.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np

from umai.analytics import safety
from umai.analytics.trend import KCAL_PER_KG, TrendPoint, trend_on

# Minimum days of history before any fit is attempted at all.
MIN_WINDOW_DAYS = 14
# Fraction of days in a window that must have adequate logging.
MIN_COVERAGE = 0.80
# Distinct windows needed before k and expenditure can be told apart.
MIN_WINDOWS = 3
# How far the state moves toward each new fit.
LEARNING_RATE = 0.35
# Strength of the pull toward the priors, in units of the design matrix.
RIDGE = 0.5

# Activity is a known OFFSET, not a fitted multiplier.
#
# The energy cost of a step scales with the mass being carried, not with basal
# rate, so modelling it as `bmr x multiplier` couples two things that are not
# coupled and lets the fit trade the logging factor against expenditure. Fixing
# the step cost at a literature value and moving it to the known side of the
# equation leaves exactly two unknowns and a well-conditioned design matrix.
KCAL_PER_STEP_PER_KG = 0.0005
# The step count that `tdee_base` is defined at, so the fitted number is a
# recognisable daily total rather than an extrapolated resting value.
REFERENCE_STEPS = 8_000
# The BMR→TDEE multiplier is applied as a bare `* 1.4` literal in
# core/tools.py current_target(). A named constant here was never imported
# and created a maintenance trap — do not re-add it.


@dataclass(frozen=True, slots=True)
class DayObservation:
    date: dt.date
    kcal_reported: float
    coverage: float
    steps: int | None = None


@dataclass(frozen=True, slots=True)
class CalibrationFit:
    k_factor: float
    tdee_estimate: float
    k_ci_low: float
    k_ci_high: float
    tdee_ci_low: float
    tdee_ci_high: float
    coverage: float
    window_days: int
    n_windows: int
    applied: bool
    reason: str


def activity_offset_kcal(steps: int | None, weight_kg: float) -> float:
    """Expenditure above or below the reference day, from step count.

    Known rather than fitted. A day of 12,000 steps costs an 88kg person about
    175 kcal more than a day of 8,000, and that is arithmetic, not something the
    engine needs to discover.

    Missing steps return zero, meaning "assume a typical day", which is the
    correct thing to assume when the phone was in a drawer.
    """
    if steps is None:
        return 0.0
    return (steps - REFERENCE_STEPS) * KCAL_PER_STEP_PER_KG * weight_kg


@dataclass(frozen=True, slots=True)
class _Window:
    reported: float
    activity_kcal: float
    observed_balance: float
    coverage: float
    days: int


def _windows(
    days: list[DayObservation],
    trend: list[TrendPoint],
    window_days: int,
    weight_kg: float,
    step_days: int = 7,
) -> list[_Window]:
    """Overlapping windows, each contributing one equation.

    Overlap costs nothing and buys resolution: with a fortnightly window and a
    weekly step, a month of data yields three equations instead of two.
    """
    by_date = {d.date: d for d in sorted(days, key=lambda d: d.date)}
    if not by_date:
        return []

    ordered = sorted(by_date)
    first, last = ordered[0], ordered[-1]

    out: list[_Window] = []
    start = first
    while start + dt.timedelta(days=window_days) <= last:
        end = start + dt.timedelta(days=window_days)
        span = [by_date[d] for d in ordered if start <= d < end]

        if len(span) >= window_days * MIN_COVERAGE:
            balance = _observed_balance(trend, start, end)
            if balance is not None:
                coverage = sum(d.coverage for d in span) / window_days
                if coverage >= MIN_COVERAGE:
                    out.append(
                        _Window(
                            reported=sum(d.kcal_reported for d in span),
                            activity_kcal=sum(
                                activity_offset_kcal(d.steps, weight_kg) for d in span
                            ),
                            observed_balance=balance,
                            coverage=coverage,
                            days=len(span),
                        )
                    )
        start += dt.timedelta(days=step_days)

    return out


def _observed_balance(trend: list[TrendPoint], start: dt.date, end: dt.date) -> float | None:
    a = trend_on(trend, start)
    b = trend_on(trend, end)
    if a is None or b is None:
        return None
    return (b - a) * KCAL_PER_KG


def fit(
    days: list[DayObservation],
    trend: list[TrendPoint],
    *,
    prior_k: float = 1.0,
    prior_tdee: float,
    weight_kg: float,
    window_days: int = MIN_WINDOW_DAYS,
) -> CalibrationFit:
    """Fit k and daily expenditure from observed history.

    `prior_tdee` is the formula estimate (Mifflin-St Jeor times an activity
    guess), used only to seed the regularisation. `weight_kg` prices the step
    offset. What comes back is expenditure on a reference day of 8,000 steps.
    """
    refused = _refusal(days, trend, prior_k, prior_tdee, window_days)
    if refused is not None:
        return refused

    windows = _windows(days, trend, window_days, weight_kg)
    if len(windows) < MIN_WINDOWS:
        return _not_yet(
            prior_k,
            prior_tdee,
            window_days,
            len(windows),
            f"only {len(windows)} usable windows; {MIN_WINDOWS} needed to separate "
            "logging bias from expenditure",
        )

    # balance = k * reported - tdee * days - activity_offset
    #
    # The activity offset is known, so it moves to the observed side and the
    # design matrix carries only the two genuine unknowns.
    a = np.array([[w.reported, -w.days] for w in windows], dtype=float)
    y = np.array([w.observed_balance + w.activity_kcal for w in windows], dtype=float)
    weights = np.array([w.coverage for w in windows], dtype=float)

    # Scale the columns so the two unknowns are comparable in magnitude, which
    # keeps the ridge term from silently regularising one far harder than the
    # other. Intake sums are ~30000 and day counts are ~14, so without this the
    # penalty is meaningless.
    scale = np.abs(a).mean(axis=0)
    scale[scale == 0] = 1.0
    a_scaled = a / scale
    prior = np.array([prior_k, prior_tdee]) * scale

    sw = np.sqrt(weights)[:, None]
    # A fixed prior weight, NOT scaled by the number of windows. Scaling it with
    # the data would mean the prior never washes out, and the fit would sit
    # permanently between the truth and the formula estimate no matter how much
    # history accumulated.
    ridge = np.sqrt(RIDGE) * np.eye(2)

    lhs = np.vstack([a_scaled * sw, ridge])
    rhs = np.concatenate([y * np.sqrt(weights), ridge @ prior])

    solution, *_ = np.linalg.lstsq(lhs, rhs, rcond=None)
    k_raw, tdee_raw = solution / scale

    residuals = a @ np.array([k_raw, tdee_raw]) - y
    k_lo, k_hi, t_lo, t_hi = _intervals(lhs, residuals, scale, k_raw, tdee_raw, len(windows))

    coverage = float(np.mean(weights))
    return CalibrationFit(
        k_factor=safety.clamp_k(float(k_raw)),
        tdee_estimate=safety.clamp_tdee(float(tdee_raw)),
        k_ci_low=safety.clamp_k(k_lo),
        k_ci_high=safety.clamp_k(k_hi),
        tdee_ci_low=safety.clamp_tdee(t_lo),
        tdee_ci_high=safety.clamp_tdee(t_hi),
        coverage=coverage,
        window_days=window_days,
        n_windows=len(windows),
        applied=True,
        reason=f"fitted over {len(windows)} windows, mean coverage {coverage:.0%}",
    )


def _intervals(
    lhs: np.ndarray,
    residuals: np.ndarray,
    scale: np.ndarray,
    k: float,
    bmr: float,
    n: int,
) -> tuple[float, float, float, float]:
    """Rough 95% intervals from the least-squares covariance.

    Honest about being rough: the windows overlap, so the residuals are
    correlated and these are narrower than the truth. They are carried because
    a target derived from a wide interval should be presented differently from
    one derived from a tight one, not because they are publication-grade.
    """
    dof = max(n - 2, 1)
    sigma_sq = float(residuals @ residuals) / dof
    try:
        cov = np.linalg.inv(lhs.T @ lhs) * sigma_sq
    except np.linalg.LinAlgError:
        return k, k, bmr, bmr
    se = np.sqrt(np.abs(np.diag(cov))) / scale
    return (k - 1.96 * se[0], k + 1.96 * se[0], bmr - 1.96 * se[1], bmr + 1.96 * se[1])


def _refusal(
    days: list[DayObservation],
    trend: list[TrendPoint],
    prior_k: float,
    prior_tdee: float,
    window_days: int,
) -> CalibrationFit | None:
    """The checks that stop a fit before it starts. Order matters: report the
    most actionable problem, not the first one encountered."""
    if len(days) < window_days:
        return _not_yet(
            prior_k,
            prior_tdee,
            window_days,
            0,
            f"{len(days)} days of history; {window_days} needed",
        )
    if len(trend) < 2:
        return _not_yet(
            prior_k,
            prior_tdee,
            window_days,
            0,
            "not enough weight readings to establish a trend",
        )
    coverage = sum(d.coverage for d in days) / len(days)
    if coverage < MIN_COVERAGE:
        return _not_yet(
            prior_k,
            prior_tdee,
            window_days,
            0,
            f"logging coverage {coverage:.0%} is below the {MIN_COVERAGE:.0%} "
            "needed for a trustworthy fit",
        )
    return None


def _not_yet(
    prior_k: float, prior_tdee: float, window_days: int, n_windows: int, reason: str
) -> CalibrationFit:
    """A refusal is a first-class result, not an exception.

    The system must be able to say "I do not know yet" and keep using the
    formula estimate, which is what `applied=False` means downstream.
    """
    return CalibrationFit(
        k_factor=prior_k,
        tdee_estimate=safety.clamp_tdee(prior_tdee),
        k_ci_low=safety.K_MIN,
        k_ci_high=safety.K_MAX,
        tdee_ci_low=safety.TDEE_MIN,
        tdee_ci_high=safety.TDEE_MAX,
        coverage=0.0,
        window_days=window_days,
        n_windows=n_windows,
        applied=False,
        reason=reason,
    )


def reported_intake_target(fit_result: CalibrationFit, daily_delta_kcal: float) -> float:
    """How many kcal you should LOG per day to achieve `daily_delta_kcal`.

    The supported output of this module, and the only combination of k and
    expenditure that the fit determines reliably (see the module docstring).

    Expressed in reported units, because that is what the user controls. If you
    under-report by a quarter, the number to aim at is not your true intake and
    telling you your true intake would be useless: you have no way to hit it.
    """
    if fit_result.k_factor <= 0:
        raise ValueError("k must be positive")
    return (fit_result.tdee_estimate + daily_delta_kcal) / fit_result.k_factor


def predicted_weekly_change_kg(fit_result: CalibrationFit, reported_kcal_per_day: float) -> float:
    """What the trend should do if you log this much per day.

    The inverse of the above, and the thing to check a fit against: a fit that
    predicts the next fortnight correctly is useful regardless of whether its
    individual parameters are right.
    """
    daily_balance = fit_result.k_factor * reported_kcal_per_day - fit_result.tdee_estimate
    return daily_balance * 7.0 / KCAL_PER_KG


def damped_update(current: float, fitted: float, rate: float = LEARNING_RATE) -> float:
    """Move part of the way to the new fit.

    One bad week should not swing the model. Two consecutive weeks pointing the
    same way should move it noticeably, which a rate around a third achieves.
    """
    return current + rate * (fitted - current)


def apply_update(
    previous: CalibrationFit | None, new: CalibrationFit, *, rate: float = LEARNING_RATE
) -> CalibrationFit:
    """Blend a new fit into the running state."""
    if previous is None or not previous.applied or not new.applied:
        return new
    return CalibrationFit(
        k_factor=safety.clamp_k(damped_update(previous.k_factor, new.k_factor, rate)),
        tdee_estimate=safety.clamp_tdee(
            damped_update(previous.tdee_estimate, new.tdee_estimate, rate)
        ),
        k_ci_low=new.k_ci_low,
        k_ci_high=new.k_ci_high,
        tdee_ci_low=new.tdee_ci_low,
        tdee_ci_high=new.tdee_ci_high,
        coverage=new.coverage,
        window_days=new.window_days,
        n_windows=new.n_windows,
        applied=True,
        reason=new.reason,
    )


# ---------------------------------------------------------------------------


def explain_plateau(
    *,
    fit_now: CalibrationFit,
    fit_before: CalibrationFit | None,
    trend_rate_kg_per_week: float | None,
) -> str:
    """Which of the three real causes is this.

    The plan is specific that when progress stalls the system must distinguish
    between them and say which it thinks is happening, and that it must check
    its own bias term before blaming the user.
    """
    if trend_rate_kg_per_week is not None and trend_rate_kg_per_week < -0.05:
        return (
            "The trend has not actually stalled. It is still moving down at "
            f"{abs(trend_rate_kg_per_week):.2f} kg/week; a flat scale reading for a few "
            "days is usually water."
        )

    if fit_before is not None and fit_before.applied and fit_now.applied:
        k_moved = fit_now.k_factor - fit_before.k_factor
        tdee_moved = fit_now.tdee_estimate - fit_before.tdee_estimate
        if k_moved > 0.05:
            return (
                f"My logging factor moved from {fit_before.k_factor:.2f} to "
                f"{fit_now.k_factor:.2f}, which means the numbers you are logging are "
                "further below what you are actually eating than they were. That is "
                "the most likely cause, and it is a measurement problem rather than "
                "a discipline one."
            )
        if tdee_moved < -75:
            return (
                f"My estimate of your expenditure has fallen from {fit_before.tdee_estimate:.0f} "
                f"to {fit_now.tdee_estimate:.0f} kcal/day. That is either less movement than "
                "before or adaptation to the deficit."
            )

    if not fit_now.applied:
        return f"I cannot tell yet: {fit_now.reason}."

    return (
        "Intake and expenditure both look stable, so the deficit itself is likely "
        "smaller than intended rather than something having changed."
    )
