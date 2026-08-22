"""Hard constraints, checked before any target or message is issued.

These live in code rather than in a prompt precisely because they exist for the
case where everything else is wrong. A prompt-level floor is a request; this is
a floor.

Every function here is total: given any input, including absurd input from a
broken calibration fit, it returns something safe or raises. None of them may
ever be made configurable from chat.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

Sex = Literal["male", "female"]

# Absolute floors, regardless of what the arithmetic suggests.
ABSOLUTE_FLOOR_KCAL: dict[str, float] = {"female": 1200.0, "male": 1500.0}

# Maximum recommended rate of loss, as a fraction of body weight per week.
MAX_LOSS_FRACTION_PER_WEEK = 0.01

# Protein floor during a deficit, g per kg of body weight, for muscle retention.
PROTEIN_G_PER_KG_DEFICIT = 1.6


@dataclass(frozen=True, slots=True)
class TargetDecision:
    kcal_target: float
    protein_target_g: float
    rationale: str
    clamped: bool
    clamp_reasons: tuple[str, ...] = ()


def mifflin_st_jeor(*, sex: Sex, weight_kg: float, height_cm: float, age_years: float) -> float:
    """Basal metabolic rate. The seed for expenditure before any data exists.

    Replaced by the calibration engine's fitted estimate as soon as there is
    enough history; this is only ever the starting guess.
    """
    if weight_kg <= 0 or height_cm <= 0 or age_years < 0:
        raise ValueError("implausible body statistics")
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age_years
    return base + (5.0 if sex == "male" else -161.0)


def age_years(birth_date: dt.date, on: dt.date) -> float:
    return (on - birth_date).days / 365.25


def max_weekly_loss_kg(weight_kg: float) -> float:
    return weight_kg * MAX_LOSS_FRACTION_PER_WEEK


def protein_floor_g(weight_kg: float) -> float:
    return weight_kg * PROTEIN_G_PER_KG_DEFICIT


def floor_kcal(sex: Sex, bmr: float) -> float:
    """The calorie target may never go below BMR, nor below the absolute floor.

    Both, not either: BMR protects a large person whose absolute floor would be
    far too low, and the absolute floor protects a small person whose computed
    BMR is below it.
    """
    return max(bmr, ABSOLUTE_FLOOR_KCAL[sex])


def decide_target(
    *,
    sex: Sex,
    weight_kg: float,
    height_cm: float,
    age: float,
    tdee_estimate: float,
    goal_rate_kg_per_week: float,
    logging_bias_k: float = 1.0,
) -> TargetDecision:
    """Turn an expenditure estimate and a goal into a safe target.

    `logging_bias_k` is the calibration engine's factor: if you consistently
    report 78% of what you eat, the number the bot shows you must be scaled so
    that hitting it produces the intended deficit. This is the whole point of
    the calibration engine, and it is applied here, once, rather than scattered.
    """
    bmr = mifflin_st_jeor(sex=sex, weight_kg=weight_kg, height_cm=height_cm, age_years=age)
    reasons: list[str] = []

    rate = goal_rate_kg_per_week
    cap = max_weekly_loss_kg(weight_kg)
    if rate < -cap:
        rate = -cap
        reasons.append(
            f"requested loss rate exceeds 1% of body weight per week; capped at {cap:.2f} kg"
        )

    daily_delta = rate * 7700.0 / 7.0
    true_target = tdee_estimate + daily_delta

    floor = floor_kcal(sex, bmr)
    if true_target < floor:
        true_target = floor
        reasons.append(
            f"target would fall below the floor of {floor:.0f} kcal "
            f"(BMR {bmr:.0f}, absolute {ABSOLUTE_FLOOR_KCAL[sex]:.0f})"
        )

    # The user logs in reported units, so the target is expressed in the same
    # units. k below 1 means under-reporting, so the shown target is lower.
    shown_target = true_target * clamp_k(logging_bias_k)

    return TargetDecision(
        kcal_target=round(shown_target),
        protein_target_g=round(protein_floor_g(weight_kg)),
        rationale=(
            f"expenditure {tdee_estimate:.0f} kcal, goal {rate:+.2f} kg/week "
            f"({daily_delta:+.0f} kcal/day), logging factor {clamp_k(logging_bias_k):.2f}"
        ),
        clamped=bool(reasons),
        clamp_reasons=tuple(reasons),
    )


# --- calibration guardrails -------------------------------------------------

K_MIN, K_MAX = 0.7, 1.4
"""Hard clamps on the logging bias factor.

Outside this range the more likely explanation is a broken fit (a stretch of
missing logs, a scale change, water retention read as fat) than a person whose
reporting is off by more than 40%. The plan names these numbers explicitly.
"""

TDEE_MIN, TDEE_MAX = 1000.0, 6000.0


def clamp_k(k: float) -> float:
    return min(max(k, K_MIN), K_MAX)


def clamp_tdee(tdee: float) -> float:
    return min(max(tdee, TDEE_MIN), TDEE_MAX)


def is_plausible_weight(kg: float) -> bool:
    return 20.0 < kg < 400.0


# --- concerning patterns ----------------------------------------------------

CONCERN_PATTERNS = (
    "obsessive re-logging",
    "sustained extreme restriction",
    "compensatory exercise immediately after eating",
    "self-critical language",
)


@dataclass(frozen=True, slots=True)
class WelfareCheck:
    soften: bool
    suggest_professional: bool
    reasons: tuple[str, ...]


def welfare_check(
    *,
    days_below_floor: int,
    corrections_today: int,
    exercise_within_30min_of_meal_count: int,
) -> WelfareCheck:
    """Detect patterns that mean the coaching should back off, not push harder.

    Deliberately conservative and deliberately blunt. When this fires, logging
    pressure is reduced rather than increased.
    """
    reasons: list[str] = []
    if days_below_floor >= 3:
        reasons.append("intake below the safety floor on three or more recent days")
    if corrections_today >= 8:
        reasons.append("unusually high re-logging today")
    if exercise_within_30min_of_meal_count >= 3:
        reasons.append("exercise logged immediately after eating, repeatedly")

    return WelfareCheck(
        soften=bool(reasons),
        suggest_professional=days_below_floor >= 7 or len(reasons) >= 2,
        reasons=tuple(reasons),
    )
