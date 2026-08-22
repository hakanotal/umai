"""The correlation engine, and above all the noise test.

Generate pure random data, run the engine, assert it reports nothing. If it
finds a pattern in noise it will find patterns in the user, and that is the
failure mode that destroys trust in a health assistant faster than any bug.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from umai.analytics.correlations import (
    MIN_ABS_R,
    Series,
    analyse,
    p_value_for,
    pearson,
)

START = dt.date(2026, 1, 1)


def _series(name: str, values: list[float]) -> Series:
    return Series(name, {START + dt.timedelta(days=i): v for i, v in enumerate(values)})


def _noise(name: str, n: int, rng: random.Random) -> Series:
    return _series(name, [rng.gauss(0, 1) for _ in range(n)])


# --- the property that matters ---------------------------------------------


@pytest.mark.parametrize("seed", range(50))
def test_pure_noise_produces_no_insights(seed):
    """Fifty independent random datasets, none of which may yield a finding.

    Run at this many seeds deliberately: a single random dataset passing proves
    nothing, and the whole point is the false positive rate across many.
    """
    rng = random.Random(seed)
    series = {
        name: _noise(name, 120, rng)
        for name in (
            "sleep_minutes",
            "kcal_reported",
            "steps",
            "protein_g",
            "satiety",
            "water_ml",
            "weight_change",
        )
    }
    assert analyse(series) == []


def test_a_real_relationship_is_found():
    """The engine must not be so conservative that it never speaks."""
    rng = random.Random(0)
    sleep = [rng.gauss(420, 45) for _ in range(90)]
    # A strong, genuine association: more sleep, less eaten.
    intake = [2400 - 1.6 * s + rng.gauss(0, 120) for s in sleep]

    findings = analyse(
        {
            "sleep_minutes": _series("sleep_minutes", sleep),
            "kcal_reported": _series("kcal_reported", intake),
        }
    )

    assert findings, "a real effect this strong must survive the gates"
    top = findings[0]
    # With sleep SD 45 and residual noise 120 the population r is about -0.51,
    # so a sample of 90 lands near -0.4. Asserting the sign and that it cleared
    # the effect-size gate is the real claim; asserting a tighter number would
    # only be asserting this seed.
    assert top.r < -MIN_ABS_R
    assert top.p_value < 0.001
    assert "not a cause" in top.statement


# --- each gate individually -------------------------------------------------


def test_small_samples_are_refused_however_strong():
    # A perfect correlation over 10 days is still 10 days.
    xs = list(range(10))
    findings = analyse(
        {
            "sleep_minutes": _series("sleep_minutes", [float(x) for x in xs]),
            "kcal_reported": _series("kcal_reported", [float(x) for x in xs]),
        }
    )
    assert findings == []


def test_weak_effects_are_refused_however_significant():
    """With enough data a trivial effect becomes significant. It is still trivial."""
    rng = random.Random(1)
    n = 4000
    sleep = [rng.gauss(0, 1) for _ in range(n)]
    intake = [0.12 * s + rng.gauss(0, 1) for s in sleep]  # r ~ 0.12

    findings = analyse(
        {
            "sleep_minutes": _series("sleep_minutes", sleep),
            "kcal_reported": _series("kcal_reported", intake),
        }
    )
    assert findings == [], "significance is not the same as mattering"


def test_undeclared_pairings_are_never_tested():
    """No dredging: a pairing absent from the declared list cannot be reported
    even when the association is overwhelming."""
    xs = [float(i) for i in range(100)]
    findings = analyse(
        {
            "mood": _series("mood", xs),
            "water_ml": _series("water_ml", xs),
        }
    )
    assert findings == []


def test_gaps_are_not_imputed():
    """Only days present in both series are paired.

    Filling gaps would manufacture the correlation being tested for, so a
    fortnight missing from one series must shrink n rather than be interpolated
    away.
    """
    rng = random.Random(7)
    sleep = [rng.gauss(420, 45) for _ in range(90)]
    intake = [2400 - 1.6 * s + rng.gauss(0, 120) for s in sleep]

    full = Series("sleep_minutes", {START + dt.timedelta(days=i): v for i, v in enumerate(sleep)})
    # A fortnight of missing intake logs in the middle.
    gapped = Series(
        "kcal_reported",
        {START + dt.timedelta(days=i): v for i, v in enumerate(intake) if not (30 <= i < 44)},
    )

    findings = analyse({"sleep_minutes": full, "kcal_reported": gapped})
    assert findings
    # 90 days minus the 14 missing, at lag 0.
    assert findings[0].n == 76


# --- the statistics themselves ---------------------------------------------


def test_pearson_endpoints():
    assert pearson([1, 2, 3], [1, 2, 3]) == pytest.approx(1.0)
    assert pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)


def test_pearson_undefined_on_a_constant_series():
    # A flat series has no variance to correlate with; None, not a crash.
    assert pearson([1, 1, 1], [1, 2, 3]) is None
    assert pearson([1], [1]) is None


def test_p_value_falls_as_evidence_grows():
    assert p_value_for(0.5, 100) < p_value_for(0.5, 20)
    assert p_value_for(0.9, 50) < p_value_for(0.3, 50)


def test_effect_size_threshold_is_where_it_claims_to_be():
    assert MIN_ABS_R == 0.30
