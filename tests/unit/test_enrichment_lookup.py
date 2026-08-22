"""The FNDDS lookup paths through the enrichment validator.

`validate` now accepts three kinds of answer, and the whole point of separating
them is that they are trusted differently. These tests pin the differences,
because every one of them is a decision someone could plausibly undo:

  * a copied row is measured USDA data and is NOT subject to the Atwater check,
    which real USDA rows fail for real reasons (alcohol, fibre);
  * a recalled composition still is, because that check is the only thing
    standing between an invention and the user's calorie total;
  * and neither path may use an fdc_id the model was not actually shown, which
    is what stops "copy" from becoming "recite a number and call it USDA".
"""

from __future__ import annotations

import pytest

from umai.core.enrichment import (
    ATWATER_ABS_TOLERANCE,
    ATWATER_REL_TOLERANCE,
    Rejected,
    validate,
)
from umai.db.models import FnddsFood, FoodSource, FoodState


def row(fdc_id: int, description: str, kcal, protein, carbs, fat, fiber=0.0) -> FnddsFood:
    """An FNDDS row as `search` would have returned it, unattached to a session."""
    return FnddsFood(
        fdc_id=fdc_id,
        food_code=str(fdc_id),
        description=description,
        wweia_category=None,
        kcal_per_100g=kcal,
        protein_g_per_100g=protein,
        carbs_g_per_100g=carbs,
        fat_g_per_100g=fat,
        fiber_g_per_100g=fiber,
        sugar_g_per_100g=None,
        sodium_mg_per_100g=None,
    )


# Real rows, transcribed from data/fndds_seed.csv.
LENTIL_SOUP = row(2708000, "Soup, lentil", 60.0, 3.9, 9.7, 0.9, 2.2)
GROUND_BEEF = row(2709000, "Beef, ground", 261.0, 25.7, 0.0, 17.0, 0.0)
FLATBREAD = row(2707000, "Bread, flat", 275.0, 9.0, 52.0, 3.0, 2.5)
# The three shapes that make Atwater the wrong gate for measured data. All are
# real rows from data/fndds_seed.csv, and all three miss the identity by more
# than the validator's tolerance while being perfectly good USDA measurements.
WHISKEY = row(2710700, "Whiskey", 231.0, 0.0, 0.0, 0.0, 0.0)  # energy is ethanol's
COCOA = row(2705587, "Cocoa powder, not reconstituted", 228.0, 19.6, 57.9, 13.7, 37.0)  # fibre
STEVIA = row(2710264, "Sugar substitute, stevia, powder", 0.0, 0.0, 100.0, 0.0, 0.0)  # polyols
LARD = row(2710166, "Lard", 902.0, 0.0, 0.0, 100.0, 0.0)  # above the old 900 ceiling


def answer(**overrides):
    base = {
        "is_food": True,
        "method": "copy",
        "canonical_name_en": "lentil soup",
        "aliases": ["mercimek corbasi"],
        "state": "boiled",
        "confidence": 0.8,
        "basis": "matched the soup row",
        "fdc_id": LENTIL_SOUP.fdc_id,
    }
    return base | overrides


OFFERED = {r.fdc_id: r for r in (LENTIL_SOUP, GROUND_BEEF, FLATBREAD, WHISKEY, COCOA, STEVIA, LARD)}


# --- copy ------------------------------------------------------------------


def test_copy_takes_its_numbers_from_the_table_not_the_model():
    """The model states an fdc_id; the composition comes from the row."""
    out = validate(
        # Numbers in the message are deliberately wrong. They must be ignored.
        answer(kcal_per_100g=999.0, protein_g_per_100g=88.0),
        expected_state=FoodState.boiled,
        offered=OFFERED,
    )
    assert out.kcal_per_100g == 60.0
    assert out.protein_g_per_100g == 3.9
    assert out.carbs_g_per_100g == 9.7
    assert out.fat_g_per_100g == 0.9


def test_copy_is_tier_2_and_records_the_fdc_id():
    out = validate(answer(), expected_state=FoodState.boiled, offered=OFFERED)
    assert out.trust_tier == 2
    assert out.source is FoodSource.usda_sr
    assert "2708000" in out.source_ref
    assert "Soup, lentil" in out.source_ref


def test_copy_keeps_the_name_it_was_asked_about_not_the_fndds_description():
    out = validate(
        answer(canonical_name_en="mercimek corbasi"),
        expected_state=FoodState.boiled,
        offered=OFFERED,
    )
    assert out.canonical_name_en == "mercimek corbasi"


def test_an_fdc_id_that_was_never_offered_is_refused():
    """Otherwise 'copy' degrades into reciting a number and calling it USDA."""
    with pytest.raises(Rejected, match="never returned by a search"):
        validate(answer(fdc_id=1234567), expected_state=FoodState.boiled, offered=OFFERED)


def test_copy_without_any_search_having_run_is_refused():
    with pytest.raises(Rejected, match="never returned by a search"):
        validate(answer(), expected_state=FoodState.boiled, offered={})


def test_copy_with_a_non_numeric_fdc_id_is_refused():
    with pytest.raises(Rejected, match="usable fdc_id"):
        validate(answer(fdc_id="Soup, lentil"), expected_state=FoodState.boiled, offered=OFFERED)


@pytest.mark.parametrize("measured", [WHISKEY, COCOA, STEVIA], ids=["alcohol", "fibre", "polyols"])
def test_measured_rows_that_fail_atwater_are_still_copied(measured):
    """The gate catches invention. Real food is not obliged to satisfy it.

    Whiskey is 231 kcal of ethanol with no macros at all, which 4/4/9 cannot
    see. Cocoa powder carries 37g of fibre inside carbohydrate-by-difference,
    which yields ~2 kcal/g rather than 4, so the identity overstates it by 205
    kcal. Stevia powder is 100g of polyol declared as zero energy. All three
    are genuine USDA measurements, and 40 of the 5,431 rows are like them.

    The precondition below is the point of the test: these really would be
    thrown out if the recall gate applied here.
    """
    implied = 4 * measured.protein_g_per_100g + 4 * measured.carbs_g_per_100g
    implied += 9 * measured.fat_g_per_100g
    tolerance = max(ATWATER_ABS_TOLERANCE, ATWATER_REL_TOLERANCE * measured.kcal_per_100g)
    assert abs(implied - measured.kcal_per_100g) > tolerance

    out = validate(
        answer(fdc_id=measured.fdc_id), expected_state=FoodState.unknown, offered=OFFERED
    )
    assert out.kcal_per_100g == measured.kcal_per_100g


def test_usdas_lard_row_is_not_mistaken_for_a_units_error():
    """902 kcal/100g is what USDA publishes. The ceiling must clear it."""
    out = validate(answer(fdc_id=LARD.fdc_id), expected_state=FoodState.unknown, offered=OFFERED)
    assert out.kcal_per_100g == 902.0


# --- compose ---------------------------------------------------------------


def composed(**overrides):
    base = answer(
        method="compose",
        canonical_name_en="kiymali pide",
        fdc_id=None,
        components=[
            {"fdc_id": FLATBREAD.fdc_id, "pct": 60},
            {"fdc_id": GROUND_BEEF.fdc_id, "pct": 40},
        ],
    )
    return base | overrides


def test_compose_does_the_arithmetic_in_code():
    out = validate(composed(), expected_state=FoodState.baked, offered=OFFERED)
    # 0.6 * 275 + 0.4 * 261 = 165 + 104.4
    assert out.kcal_per_100g == pytest.approx(269.4)
    assert out.protein_g_per_100g == pytest.approx(0.6 * 9.0 + 0.4 * 25.7)
    assert out.carbs_g_per_100g == pytest.approx(0.6 * 52.0)
    assert out.fat_g_per_100g == pytest.approx(0.6 * 3.0 + 0.4 * 17.0)


def test_compose_stays_tier_4_because_the_proportions_are_a_guess():
    out = validate(composed(), expected_state=FoodState.baked, offered=OFFERED)
    assert out.trust_tier == 4
    assert out.source is FoodSource.model
    assert "fndds-composed" in out.source_ref


def test_compose_normalises_shares_that_do_not_quite_reach_100():
    """49/49 is a whole dish described with rounding, not half a dish."""
    out = validate(
        composed(
            components=[
                {"fdc_id": FLATBREAD.fdc_id, "pct": 49},
                {"fdc_id": GROUND_BEEF.fdc_id, "pct": 49},
            ]
        ),
        expected_state=FoodState.baked,
        offered=OFFERED,
    )
    assert out.kcal_per_100g == pytest.approx(0.5 * 275.0 + 0.5 * 261.0)


def test_compose_refuses_shares_that_do_not_sum_to_a_whole_dish():
    with pytest.raises(Rejected, match="sum to"):
        validate(
            composed(
                components=[
                    {"fdc_id": FLATBREAD.fdc_id, "pct": 30},
                    {"fdc_id": GROUND_BEEF.fdc_id, "pct": 20},
                ]
            ),
            expected_state=FoodState.baked,
            offered=OFFERED,
        )


def test_compose_refuses_a_component_it_was_never_shown():
    with pytest.raises(Rejected, match="never returned by a search"):
        validate(
            composed(
                components=[
                    {"fdc_id": FLATBREAD.fdc_id, "pct": 60},
                    {"fdc_id": 9999999, "pct": 40},
                ]
            ),
            expected_state=FoodState.baked,
            offered=OFFERED,
        )


def test_compose_refuses_a_negative_share():
    with pytest.raises(Rejected, match="non-positive share"):
        validate(
            composed(
                components=[
                    {"fdc_id": FLATBREAD.fdc_id, "pct": 130},
                    {"fdc_id": GROUND_BEEF.fdc_id, "pct": -30},
                ]
            ),
            expected_state=FoodState.baked,
            offered=OFFERED,
        )


def test_compose_refuses_the_same_row_counted_twice():
    with pytest.raises(Rejected, match="listed twice"):
        validate(
            composed(
                components=[
                    {"fdc_id": FLATBREAD.fdc_id, "pct": 50},
                    {"fdc_id": FLATBREAD.fdc_id, "pct": 50},
                ]
            ),
            expected_state=FoodState.baked,
            offered=OFFERED,
        )


def test_compose_with_no_components_is_refused():
    with pytest.raises(Rejected, match="no components"):
        validate(composed(components=[]), expected_state=FoodState.baked, offered=OFFERED)


# --- recall ----------------------------------------------------------------


def test_recall_still_faces_the_atwater_gate():
    """The lookup paths are exempt. The invention path is emphatically not."""
    with pytest.raises(Rejected, match="Atwater"):
        validate(
            answer(
                method="recall",
                fdc_id=None,
                kcal_per_100g=500.0,
                protein_g_per_100g=5.0,
                carbs_g_per_100g=10.0,
                fat_g_per_100g=2.0,
            ),
            expected_state=FoodState.unknown,
            offered=OFFERED,
        )


def test_recall_needs_an_energy_figure():
    with pytest.raises(Rejected, match="without kcal"):
        validate(
            answer(method="recall", fdc_id=None),
            expected_state=FoodState.unknown,
            offered=OFFERED,
        )


def test_a_response_with_no_method_is_treated_as_recall():
    """Backwards compatible: the field is new, and an older-shaped answer that
    carries its own numbers is exactly what `recall` means."""
    raw = answer(
        kcal_per_100g=250.0,
        protein_g_per_100g=11.0,
        carbs_g_per_100g=32.0,
        fat_g_per_100g=8.0,
    )
    del raw["method"]
    del raw["fdc_id"]
    out = validate(raw, expected_state=FoodState.unknown, offered=OFFERED)
    assert out.method == "recall"
    assert out.trust_tier == 4


def test_an_unknown_method_is_refused():
    with pytest.raises(Rejected, match="unknown method"):
        validate(answer(method="guess"), expected_state=FoodState.unknown, offered=OFFERED)
