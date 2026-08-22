#!/usr/bin/env python3
"""Synthetic history generator and calibration simulator.

Produces a plausible person with a KNOWN true intake and a KNOWN logging bias,
then replays that history through the real analytics code and checks it recovers
what was planted.

This is the highest-value test in the project. It answers the question the whole
design rests on, which is whether the calibration engine actually converges, and
it answers it in about a second instead of two months. It also lets the learning
rate, window length and clamps be tuned against something measurable rather than
by intuition.

    uv run python tools/simulate.py generate --days 90 --true-tdee 2400 \
        --logging-bias 0.78 --seed 42
    uv run python tools/simulate.py run --history sim_90d.json
    uv run python tools/simulate.py hostile

The hostile mode is the important one. A calibration engine that converges on
clean data and produces nonsense on a holiday is not finished.

Scored on the reported-intake target rather than on recovering k, because k and
expenditure are collinear and only their combination is identified. See the
calibration module docstring.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from umai.analytics.calibration import (
    CalibrationFit,
    DayObservation,
    apply_update,
    fit,
    reported_intake_target,
)
from umai.analytics.trend import KCAL_PER_KG, ewma

# Day-to-day scale noise: water, glycogen, gut contents. Around 0.6 kg SD is
# typical of real smart-scale data and is exactly what the trend exists to see
# through.
SCALE_NOISE_KG = 0.6


@dataclass
class Day:
    date: str
    true_intake: float
    reported_intake: float
    true_expenditure: float
    weight_kg: float | None
    steps: int
    coverage: float


@dataclass
class History:
    true_tdee: float
    true_k: float
    start_weight_kg: float
    days: list[Day]

    @property
    def true_logging_bias(self) -> float:
        """What fraction of intake the person reports. k is its inverse."""
        return 1.0 / self.true_k


def generate(
    *,
    days: int,
    true_tdee: float,
    logging_bias: float,
    start_weight_kg: float = 88.0,
    seed: int = 42,
    start: dt.date | None = None,
    skip_weeks: tuple[int, ...] = (),
    holiday_week: int | None = None,
    water_retention_week: int | None = None,
    drift_tdee_per_week: float = 0.0,
    weigh_probability: float = 0.85,
) -> History:
    """A plausible person with known parameters.

    `logging_bias` is what fraction of true intake they report: 0.78 means they
    log 78% of what they eat, so the factor the engine must recover is 1/0.78,
    about 1.28.
    """
    rng = random.Random(seed)
    start = start or dt.date(2026, 1, 1)

    weight = start_weight_kg
    out: list[Day] = []

    for i in range(days):
        day = start + dt.timedelta(days=i)
        week = i // 7

        expenditure = true_tdee + drift_tdee_per_week * week
        steps = max(0, int(rng.gauss(8000, 2500)))
        # Movement and expenditure are correlated, which is what makes the
        # activity multiplier worth having at all.
        expenditure += (steps - 8000) * 0.04

        target_intake = expenditure - 500  # a deliberate deficit
        intake = rng.gauss(target_intake, 350)

        if holiday_week is not None and week == holiday_week:
            intake += rng.gauss(900, 200)  # a week of eating out

        # Energy balance drives real weight; noise is added at the scale, not here.
        weight += (intake - expenditure) / KCAL_PER_KG

        observed = weight
        if water_retention_week is not None and week == water_retention_week:
            observed += 1.2  # salt and glycogen, masking real loss

        logged = week not in skip_weeks
        coverage = 1.0 if logged else 0.0
        reported = intake * logging_bias if logged else 0.0
        if logged:
            # Reporting is noisy as well as biased, and the noise is what makes
            # the fit non-trivial.
            reported *= rng.gauss(1.0, 0.08)

        weighed = rng.random() < weigh_probability
        out.append(
            Day(
                date=day.isoformat(),
                true_intake=round(intake, 1),
                reported_intake=round(reported, 1),
                true_expenditure=round(expenditure, 1),
                weight_kg=round(observed + rng.gauss(0, SCALE_NOISE_KG), 2) if weighed else None,
                steps=steps,
                coverage=coverage,
            )
        )

    return History(
        true_tdee=true_tdee,
        true_k=1.0 / logging_bias,
        start_weight_kg=start_weight_kg,
        days=out,
    )


def _to_inputs(history: History, upto: int) -> tuple[list[DayObservation], list]:
    span = history.days[:upto]
    observations = [
        DayObservation(
            date=dt.date.fromisoformat(d.date),
            kcal_reported=d.reported_intake,
            coverage=d.coverage,
            steps=d.steps,
        )
        for d in span
        if d.coverage > 0
    ]
    readings = [
        (dt.date.fromisoformat(d.date), d.weight_kg) for d in span if d.weight_kg is not None
    ]
    return observations, ewma(readings)


def run(history: History, *, prior_tdee: float = 2100.0, quiet: bool = False) -> dict:
    """Replay the history through the real engine, a fortnight at a time."""
    state: CalibrationFit | None = None
    rows: list[dict] = []

    for upto in range(14, len(history.days) + 1, 14):
        observations, trend = _to_inputs(history, upto)
        new = fit(
            observations,
            trend,
            prior_tdee=prior_tdee,
            weight_kg=history.start_weight_kg,
        )
        state = apply_update(state, new)

        rows.append(
            {
                "day": upto,
                "k": round(state.k_factor, 3),
                "tdee": round(state.tdee_estimate),
                "applied": state.applied,
                "windows": state.n_windows,
                "reason": state.reason,
            }
        )
        if not quiet:
            status = (
                f"k={state.k_factor:.2f}  tdee={state.tdee_estimate:.0f}"
                if state.applied
                else f"no fit ({state.reason})"
            )
            print(f"  day {upto:3d}   {status}")

    assert state is not None

    # The headline metric is the ACTIONABLE one, not parameter recovery.
    #
    # k and expenditure are collinear and individually poorly identified; the
    # target derived from them is not. Scoring this on parameter recovery would
    # fail a fit that gives correct advice, and pass one that does not.
    deficit = -500.0
    true_target = (history.true_tdee + deficit) / history.true_k
    got_target = reported_intake_target(state, deficit)
    target_err = abs(got_target - true_target) / true_target

    k_err = abs(state.k_factor - history.true_k) / history.true_k
    tdee_err = abs(state.tdee_estimate - history.true_tdee) / history.true_tdee

    if not quiet:
        print(
            f"\n  target to log for a 500 kcal/day deficit:"
            f"  {got_target:.0f} kcal   (true {true_target:.0f}, err {target_err:.1%})"
            f"\n  parameters k={state.k_factor:.3f} (true {history.true_k:.3f}), "
            f"tdee={state.tdee_estimate:.0f} (true {history.true_tdee:.0f})"
            f"\n  individually off by {k_err:.0%} and {tdee_err:.0%}; they are collinear "
            f"and only the combination is identified."
        )

    return {
        "final_k": state.k_factor,
        "final_tdee": state.tdee_estimate,
        "true_k": history.true_k,
        "true_tdee": history.true_tdee,
        "target": got_target,
        "true_target": true_target,
        "target_error": target_err,
        "k_error": k_err,
        "tdee_error": tdee_err,
        "applied": state.applied,
        "rows": rows,
    }


HOSTILE = {
    "a week of no logging": {"skip_weeks": (5,)},
    "a holiday week": {"holiday_week": 6},
    "water retention masking loss": {"water_retention_week": 7},
    "expenditure drifting down": {"drift_tdee_per_week": -12.0},
    "weighing twice a week": {"weigh_probability": 0.3},
    "everything at once": {
        "skip_weeks": (5,),
        "holiday_week": 6,
        "water_retention_week": 7,
        "drift_tdee_per_week": -12.0,
        "weigh_probability": 0.3,
    },
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate")
    g.add_argument("--days", type=int, default=90)
    g.add_argument("--true-tdee", type=float, default=2400)
    g.add_argument("--logging-bias", type=float, default=0.78)
    g.add_argument("--start-weight", type=float, default=88.0)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--out", default=None)

    r = sub.add_parser("run")
    r.add_argument("--history", required=True)
    r.add_argument("--prior-tdee", type=float, default=2100.0)

    h = sub.add_parser("hostile")
    h.add_argument("--days", type=int, default=90)
    h.add_argument("--seed", type=int, default=42)

    args = ap.parse_args()

    if args.cmd == "generate":
        history = generate(
            days=args.days,
            true_tdee=args.true_tdee,
            logging_bias=args.logging_bias,
            start_weight_kg=args.start_weight,
            seed=args.seed,
        )
        out = Path(args.out or f"sim_{args.days}d.json")
        out.write_text(json.dumps(asdict(history), indent=2))
        print(
            f"{args.days} days written to {out}\n"
            f"  true tdee {history.true_tdee:.0f}, true k {history.true_k:.3f} "
            f"(reports {args.logging_bias:.0%} of intake)"
        )
        return 0

    if args.cmd == "run":
        raw = json.loads(Path(args.history).read_text())
        history = History(
            true_tdee=raw["true_tdee"],
            true_k=raw["true_k"],
            start_weight_kg=raw["start_weight_kg"],
            days=[Day(**d) for d in raw["days"]],
        )
        result = run(history, prior_tdee=args.prior_tdee)
        return 0 if result["applied"] else 1

    # hostile
    print("Replaying deliberately hostile histories.\n")
    worst = 0.0
    for label, kwargs in HOSTILE.items():
        history = generate(
            days=args.days, true_tdee=2400, logging_bias=0.78, seed=args.seed, **kwargs
        )
        result = run(history, quiet=True)
        worst = max(worst, result["target_error"])
        verdict = "ok" if result["applied"] and result["target_error"] < 0.10 else "DEGRADED"
        print(
            f"  {label:32s} target={result['target']:.0f} kcal "
            f"(true {result['true_target']:.0f}, err {result['target_error']:5.1%})  {verdict}"
        )
    print(f"\n  worst target error across hostile histories: {worst:.1%}")
    return 0 if worst < 0.10 else 1


if __name__ == "__main__":
    raise SystemExit(main())
