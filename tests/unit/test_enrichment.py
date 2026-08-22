"""The gate on model-invented nutrition.

`core/enrichment.py` is the one place where a number a language model produced
becomes a calorie figure a person reads, so its validator carries more weight
than anything else in the module. These tests are the specification of what it
must refuse.

The Atwater check is the centre of it: 4·protein + 4·carbs + 9·fat has to
reconstruct the stated energy. A model confabulating a composition rarely
confabulates an internally consistent one, so the identity does most of the
work of telling recall from invention.
"""

from __future__ import annotations

import pytest

from umai.core import cuisines
from umai.core.enrichment import Rejected, atwater_kcal, validate
from umai.db.models import FoodState


def lahmacun(**overrides):
    """A real composition, near enough: thin dough, minced lamb, onion, pepper."""
    base = {
        "is_food": True,
        "canonical_name_en": "lahmacun",
        "aliases": ["turkish pizza", "lahmacun"],
        "state": "baked",
        "kcal_per_100g": 250.0,
        "protein_g_per_100g": 11.0,
        "carbs_g_per_100g": 32.0,
        "fat_g_per_100g": 8.0,
        "fiber_g_per_100g": 2.0,
        "confidence": 0.8,
        "basis": "thin dough with minced lamb, onion and pepper",
    }
    return base | overrides


def test_a_plausible_dish_is_accepted_and_normalised():
    out = validate(lahmacun(), expected_state=FoodState.unknown)
    assert out.canonical_name_en == "lahmacun"
    assert out.state is FoodState.baked
    assert out.kcal_per_100g == 250.0
    # The name is not repeated in its own alias list.
    assert "lahmacun" not in out.aliases
    assert "turkish pizza" in out.aliases


def test_the_state_falls_back_to_what_was_observed():
    out = validate(lahmacun(state="nonsense"), expected_state=FoodState.grilled)
    assert out.state is FoodState.grilled


def test_energy_that_does_not_reconcile_with_the_macros_is_rejected():
    """The load-bearing test. 11g protein, 32g carbs and 8g fat is ~244 kcal;
    a row claiming 600 has invented one of the four numbers."""
    with pytest.raises(Rejected, match="Atwater"):
        validate(lahmacun(kcal_per_100g=600.0), expected_state=FoodState.unknown)


def test_the_atwater_tolerance_admits_ordinary_rounding():
    # Real published rows disagree with their own macros by a few per cent:
    # fibre is counted differently and everything is rounded.
    implied = atwater_kcal(11.0, 32.0, 8.0)
    out = validate(lahmacun(kcal_per_100g=implied + 25), expected_state=FoodState.unknown)
    assert out.kcal_per_100g == pytest.approx(implied + 25)


def test_a_non_food_is_rejected_rather_than_guessed():
    """Perception produced "background plate" and "garnish on flatbread" as
    item names. A composition for either is pure invention."""
    with pytest.raises(Rejected, match="not a food"):
        validate(lahmacun(is_food=False), expected_state=FoodState.unknown)


def test_a_missing_is_food_flag_is_not_a_refusal():
    """The enrichment model answers in json_object mode, where a field can
    simply be absent. Reading absence as denial rejected "french fries" and
    "brisket" on one live run and accepted them on the next; the arithmetic
    gates are what actually guard this table."""
    payload = lahmacun()
    del payload["is_food"]
    assert validate(payload, expected_state=FoodState.unknown).kcal_per_100g == 250.0


def test_energy_denser_than_pure_fat_is_rejected():
    with pytest.raises(Rejected, match="exceeds pure fat"):
        validate(
            lahmacun(kcal_per_100g=4000.0, fat_g_per_100g=440.0, carbs_g_per_100g=0.0),
            expected_state=FoodState.unknown,
        )


def test_more_than_100g_of_macros_in_100g_of_food_is_rejected():
    with pytest.raises(Rejected, match="impossible"):
        validate(
            lahmacun(protein_g_per_100g=60.0, carbs_g_per_100g=60.0, fat_g_per_100g=20.0),
            expected_state=FoodState.unknown,
        )


def test_negative_macros_are_rejected():
    with pytest.raises(Rejected, match="negative"):
        validate(lahmacun(fat_g_per_100g=-8.0), expected_state=FoodState.unknown)


def test_a_diffident_model_is_not_written_into_the_food_table():
    with pytest.raises(Rejected, match="confidence"):
        validate(lahmacun(confidence=0.1), expected_state=FoodState.unknown)


def test_an_out_of_range_yield_factor_is_dropped_not_fatal():
    """The foods table has a check constraint on this. Dropping the field beats
    an IntegrityError at flush that loses the whole otherwise-good row."""
    out = validate(lahmacun(yield_factor=42.0), expected_state=FoodState.unknown)
    assert out.yield_factor is None


def test_a_caption_masquerading_as_a_name_is_rejected():
    with pytest.raises(Rejected, match="lookup key"):
        validate(lahmacun(canonical_name_en="x" * 250), expected_state=FoodState.unknown)


def test_nonsense_in_a_numeric_field_is_rejected_not_coerced():
    with pytest.raises(Rejected, match="not a number"):
        validate(lahmacun(kcal_per_100g="about three hundred"), expected_state=FoodState.unknown)


# --- the cuisine list -------------------------------------------------------


def test_cuisines_normalise_to_a_stable_order():
    """Stability matters: the list goes into the perception prompt, and an
    unstable prompt is an unstable fingerprint, which is what perception_runs
    exists to distinguish from model drift."""
    assert cuisines.normalise(["italian", "turkish"]) == cuisines.normalise(
        ["turkish", "italian"]
    )


def test_unknown_and_duplicate_cuisines_are_dropped():
    assert cuisines.normalise(["turkish", "turkish", "klingon", ""]) == ["turkish"]


def test_the_cuisine_list_is_capped():
    picked = cuisines.normalise(list(cuisines.CUISINES))
    assert len(picked) == cuisines.MAX_CUISINES


def test_no_cuisines_produces_no_hint_at_all():
    """An empty list must leave the prompt untouched rather than telling the
    model the user eats nothing."""
    assert cuisines.describe([]) == ""
    assert "Turkish" in cuisines.describe(["turkish"])
