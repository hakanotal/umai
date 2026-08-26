"""Stage 3 arithmetic.

Pure functions, trivial to test, catastrophic if wrong: every number the user
ever sees passes through here. Tested exhaustively on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from umai.resolver.compute import (
    Macros,
    absorbed_oil_grams,
    compute,
    ml_to_grams,
    per_100g,
    raw_equivalent,
    recipe_profile,
    serving,
    total,
)


@dataclass
class F:
    """A stand-in for a foods row. compute() takes a Protocol, so no ORM here."""

    canonical_name_en: str = "test food"
    kcal_per_100g: float = 100.0
    protein_g_per_100g: float = 10.0
    carbs_g_per_100g: float = 20.0
    fat_g_per_100g: float = 3.0
    fiber_g_per_100g: float | None = 1.0
    density_g_per_ml: float | None = None
    yield_factor: float | None = None
    fat_absorption_pct: float | None = None


# --- the basic multiplication ----------------------------------------------


def test_100g_of_a_row_is_the_row():
    c = compute(F(), grams=100)
    assert c.macros == Macros(100.0, 10.0, 20.0, 3.0, 1.0)


@pytest.mark.parametrize(
    ("grams", "kcal"),
    [(0, 0.0), (50, 50.0), (100, 100.0), (250, 250.0), (1000, 1000.0)],
)
def test_scales_linearly(grams, kcal):
    assert compute(F(), grams=grams).macros.kcal == pytest.approx(kcal)


def test_zero_grams_is_zero_not_an_error():
    # An entry can legitimately be nothing: a drink logged and then corrected.
    assert compute(F(), grams=0).macros.kcal == 0.0


def test_negative_weight_is_refused():
    with pytest.raises(ValueError, match="negative"):
        compute(F(), grams=-1)


def test_needs_exactly_one_of_grams_or_ml():
    with pytest.raises(ValueError, match="exactly one"):
        compute(F(), grams=100, ml=100)
    with pytest.raises(ValueError, match="exactly one"):
        compute(F())


def test_missing_fiber_reads_as_zero_not_none():
    assert compute(F(fiber_g_per_100g=None), grams=100).macros.fiber_g == 0.0


# --- density ---------------------------------------------------------------


def test_water_is_one_to_one():
    assert ml_to_grams(250, 1.0) == 250.0


@pytest.mark.parametrize(
    ("ml", "density", "grams"),
    [
        (200, 1.03, 206.0),  # milk
        (15, 0.92, 13.8),  # olive oil, one tablespoon
        (20, 1.42, 28.4),  # honey
    ],
)
def test_density_converts_volume_to_mass(ml, density, grams):
    assert ml_to_grams(ml, density) == pytest.approx(grams)


def test_absent_density_falls_back_to_water():
    # Wrong for oil, but a defined and documented wrong rather than a crash.
    assert ml_to_grams(100, None) == 100.0
    assert ml_to_grams(100, 0) == 100.0


def test_ml_path_and_grams_path_agree():
    milk = F(density_g_per_ml=1.03)
    assert compute(milk, ml=200).macros.kcal == pytest.approx(compute(milk, grams=206).macros.kcal)


def test_negative_volume_is_refused():
    with pytest.raises(ValueError, match="negative"):
        ml_to_grams(-5, 1.0)


# --- raw versus cooked: the largest hidden error source --------------------


def test_boiled_rice_is_not_raw_rice():
    """The canonical example. Raw rice is ~360 kcal/100g; if 180g of boiled rice
    were priced against the raw row directly it would read ~648 kcal instead of
    the correct ~240. A threefold error on a staple."""
    raw_rice = F(canonical_name_en="white rice", kcal_per_100g=360.0, yield_factor=2.7)

    naive = compute(raw_rice, grams=180).macros.kcal
    correct = compute(raw_rice, grams=180, from_raw_row_but_eaten_cooked=True).macros.kcal

    assert naive == pytest.approx(648.0)
    assert correct == pytest.approx(240.0, rel=0.01)
    assert naive / correct == pytest.approx(2.7, rel=0.01)


def test_meat_loses_weight_rather_than_gaining_it():
    # Roasting drives off water: yield below 1.
    assert raw_equivalent(150, 0.75) == pytest.approx(200.0)


def test_missing_yield_factor_passes_the_weight_through():
    # No factor means no correction, not a crash and not a guess.
    c = compute(F(yield_factor=None), grams=200, from_raw_row_but_eaten_cooked=True)
    assert c.grams_of_row == 200.0
    assert "no yield factor" in " ".join(c.steps)


def test_conversion_is_off_unless_asked_for():
    # A row that already describes the cooked food must not be converted again.
    cooked_row = F(kcal_per_100g=130.0, yield_factor=2.7)
    assert compute(cooked_row, grams=180).macros.kcal == pytest.approx(234.0)


# --- fat absorbed during frying --------------------------------------------


def test_frying_adds_oil_that_is_in_no_ingredient_list():
    chips = F(canonical_name_en="potato", kcal_per_100g=77.0, fat_absorption_pct=10.0)

    plain = compute(chips, grams=200).macros
    fried = compute(chips, grams=200, apply_fat_absorption=True).macros

    assert absorbed_oil_grams(200, 10.0) == 20.0
    assert fried.fat_g - plain.fat_g == pytest.approx(20.0)
    assert fried.kcal - plain.kcal == pytest.approx(176.8)  # 20g x 8.84 kcal/g


def test_absorption_is_off_by_default_to_avoid_double_counting():
    chips = F(fat_absorption_pct=10.0)
    assert compute(chips, grams=200).macros == compute(chips, grams=200).macros
    assert compute(chips, grams=200).macros.fat_g == pytest.approx(6.0)


@pytest.mark.parametrize("pct", [None, 0, -5])
def test_no_absorption_configured_means_no_addition(pct):
    assert absorbed_oil_grams(100, pct) == 0.0


# --- the two corrections together ------------------------------------------


def test_yield_and_absorption_compose_in_the_right_order():
    """Absorption is a fraction of what is on the plate, not of the raw
    equivalent: the oil is taken up by the cooked food."""
    food = F(kcal_per_100g=360.0, yield_factor=2.0, fat_absorption_pct=10.0)
    c = compute(food, grams=200, from_raw_row_but_eaten_cooked=True, apply_fat_absorption=True)

    assert c.grams_of_row == pytest.approx(100.0)  # 200g cooked / 2.0
    # 100g of row = 360 kcal, plus 20g of oil (10% of the 200g served) = 176.8
    assert c.macros.kcal == pytest.approx(536.8)


# --- summation --------------------------------------------------------------


def test_a_meal_is_the_sum_of_its_parts():
    plate = [compute(F(), grams=100), compute(F(), grams=50)]
    assert total(plate).kcal == pytest.approx(150.0)


def test_an_empty_meal_is_zero_not_an_error():
    assert total([]).kcal == 0.0


def test_macros_add_componentwise():
    a, b = Macros(1, 2, 3, 4, 5), Macros(10, 20, 30, 40, 50)
    assert a + b == Macros(11, 22, 33, 44, 55)


# --- recipes ----------------------------------------------------------------


def test_evaporation_is_what_the_cooked_weight_captures():
    """1400g of ingredients in, 1100g of soup out. Computing per-100g against
    the raw total understates every serving by the evaporation rate."""
    ingredients = [
        (F(canonical_name_en="lentils", kcal_per_100g=350.0), 300.0),
        (F(canonical_name_en="water", kcal_per_100g=0.0), 1000.0),
        (F(canonical_name_en="olive oil", kcal_per_100g=884.0), 100.0),
    ]
    total_kcal = 300 * 3.5 + 884  # 1934

    weighed = recipe_profile(ingredients, cooked_output_grams=1100.0)
    unweighed = recipe_profile(ingredients)

    assert weighed.kcal_per_100g == pytest.approx(total_kcal / 11.0, rel=1e-6)
    assert unweighed.kcal_per_100g == pytest.approx(total_kcal / 14.0, rel=1e-6)
    assert weighed.yield_factor == pytest.approx(1100 / 1400)
    # the unweighed version understates a 350g bowl by ~21%
    assert serving(unweighed, 350).macros.kcal < serving(weighed, 350).macros.kcal


def test_recipe_energy_is_conserved():
    ingredients = [(F(kcal_per_100g=200.0), 500.0), (F(kcal_per_100g=100.0), 500.0)]
    profile = recipe_profile(ingredients, cooked_output_grams=800.0)
    # eating the whole pot must equal the sum of the ingredients
    assert serving(profile, 800).macros.kcal == pytest.approx(1500.0)


def test_a_recipe_needs_ingredients():
    with pytest.raises(ValueError, match="at least one ingredient"):
        recipe_profile([])


def test_weightless_ingredients_are_refused():
    with pytest.raises(ValueError, match="weigh nothing"):
        recipe_profile([(F(), 0.0)])


# --- the audit trail --------------------------------------------------------


def test_every_computation_can_explain_itself():
    # The explain-why feature promises traceability; this is what keeps it.
    c = compute(F(density_g_per_ml=1.03), ml=200)
    assert c.steps
    assert "g/ml" in c.steps[0]
    assert "kcal" in c.steps[-1]


def test_per_100g_round_trips():
    f = F()
    assert per_100g(f) == compute(f, grams=100).macros
