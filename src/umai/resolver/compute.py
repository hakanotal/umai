"""Stage 3, arithmetic. Pure functions, no model call, no I/O.

grams x per-100g, with density for volumes, yield_factor for the raw-to-cooked
weight change, and fat_absorption_pct for oil taken up in frying. Deterministic,
auditable, reproducible and free.

Section 4.2 of the plan is a standing instruction about this module: once the
per-100g table exists, this step is multiplication, and a language model doing
it would be slower, more expensive, non-deterministic, and occasionally wrong in
ways that are invisible. Nothing here may become a model call.

Three conversions do the real work, and each of them corrects an error that is
large, systematic, and in a known direction:

  density         a millilitre is not a gram for anything but water
  yield_factor    100g of raw rice and 100g of boiled rice differ by roughly a
                  factor of three in energy, because boiled rice is mostly
                  absorbed water
  fat absorption  deep frying adds 5-15% of the food's weight in oil, which
                  appears in no ingredient list and is exactly the thing you
                  most want to track
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Energy density of absorbed cooking oil. Vegetable oils are ~884 kcal/100g and
# essentially pure fat; the difference between oils is far below the error of
# the absorption estimate itself.
OIL_KCAL_PER_100G = 884.0
OIL_FAT_G_PER_100G = 100.0

# Density of water, the default when a liquid has no measured density. Using
# 1.0 for, say, olive oil overstates its mass by 9%.
WATER_DENSITY_G_PER_ML = 1.0


class FoodLike(Protocol):
    """What compute needs from a food row.

    A Protocol rather than the ORM class so these functions stay testable with
    plain objects and can never accidentally touch the database.

    Declared read-only (properties, not attributes) so that frozen dataclasses
    satisfy it. Nothing here writes to a food row, and a protocol demanding
    mutability would exclude exactly the immutable values this module prefers.
    """

    @property
    def canonical_name_en(self) -> str: ...
    @property
    def kcal_per_100g(self) -> float: ...
    @property
    def protein_g_per_100g(self) -> float: ...
    @property
    def carbs_g_per_100g(self) -> float: ...
    @property
    def fat_g_per_100g(self) -> float: ...
    @property
    def fiber_g_per_100g(self) -> float | None: ...
    @property
    def density_g_per_ml(self) -> float | None: ...
    @property
    def yield_factor(self) -> float | None: ...
    @property
    def fat_absorption_pct(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class Macros:
    kcal: float = 0.0
    protein_g: float = 0.0
    carbs_g: float = 0.0
    fat_g: float = 0.0
    fiber_g: float = 0.0

    def __add__(self, other: Macros) -> Macros:
        return Macros(
            kcal=self.kcal + other.kcal,
            protein_g=self.protein_g + other.protein_g,
            carbs_g=self.carbs_g + other.carbs_g,
            fat_g=self.fat_g + other.fat_g,
            fiber_g=self.fiber_g + other.fiber_g,
        )

    def scaled(self, factor: float) -> Macros:
        return Macros(
            kcal=self.kcal * factor,
            protein_g=self.protein_g * factor,
            carbs_g=self.carbs_g * factor,
            fat_g=self.fat_g * factor,
            fiber_g=self.fiber_g * factor,
        )


ZERO = Macros()


@dataclass(frozen=True, slots=True)
class Computation:
    """The result, plus how it was reached.

    The trail is not decoration. The explain-why feature promises that any
    number can be traced, and a stored `steps` list is what makes that promise
    keepable without re-running anything.
    """

    macros: Macros
    grams_of_row: float
    steps: tuple[str, ...] = ()


def ml_to_grams(ml: float, density_g_per_ml: float | None) -> float:
    """Convert a volume to a mass.

    Without this every drink is wrong by a few percent, in a direction that is
    not consistent: milk is denser than water, oil is lighter.
    """
    if ml < 0:
        raise ValueError("volume cannot be negative")
    density = density_g_per_ml if density_g_per_ml and density_g_per_ml > 0 else None
    return ml * (density if density is not None else WATER_DENSITY_G_PER_ML)


def raw_equivalent(cooked_grams: float, yield_factor: float | None) -> float:
    """How much raw ingredient a cooked weight corresponds to.

    Needed when the composition row is a raw ingredient but the thing on the
    plate is cooked. Boiled rice weighs roughly 2.7x its raw weight, so 180g of
    boiled rice carries the nutrition of about 67g of raw rice.
    """
    if cooked_grams < 0:
        raise ValueError("weight cannot be negative")
    if not yield_factor or yield_factor <= 0:
        return cooked_grams
    return cooked_grams / yield_factor


def absorbed_oil_grams(food_grams: float, fat_absorption_pct: float | None) -> float:
    """Oil taken up during frying, as grams."""
    if not fat_absorption_pct or fat_absorption_pct <= 0:
        return 0.0
    return food_grams * (fat_absorption_pct / 100.0)


def per_100g(food: FoodLike) -> Macros:
    return Macros(
        kcal=food.kcal_per_100g,
        protein_g=food.protein_g_per_100g,
        carbs_g=food.carbs_g_per_100g,
        fat_g=food.fat_g_per_100g,
        fiber_g=food.fiber_g_per_100g or 0.0,
    )


def compute(
    food: FoodLike,
    *,
    grams: float | None = None,
    ml: float | None = None,
    from_raw_row_but_eaten_cooked: bool = False,
    apply_fat_absorption: bool = False,
) -> Computation:
    """Nutrition for a portion of one food.

    Exactly one of `grams` or `ml`. `ml` is converted through the row's density.

    `from_raw_row_but_eaten_cooked` says the portion was weighed cooked while
    the composition row describes the raw ingredient, so the weight is divided
    back through the yield factor before the per-100g figures are applied. It is
    a caller decision rather than something inferred here, because getting it
    wrong silently is a threefold error on staples and the resolver is the only
    layer that knows both states.

    `apply_fat_absorption` adds the oil a fried item took up. Off by default:
    applying it to a row that already describes the fried food would double count.
    """
    if (grams is None) == (ml is None):
        raise ValueError("give exactly one of grams or ml")

    steps: list[str] = []

    if ml is not None:
        density = food.density_g_per_ml
        grams = ml_to_grams(ml, density)
        steps.append(
            f"{ml:g}ml x {density if density else WATER_DENSITY_G_PER_ML:g} g/ml = {grams:.1f}g"
        )

    assert grams is not None  # narrowed by the check above
    if grams < 0:
        raise ValueError("weight cannot be negative")

    portion_grams = grams
    if from_raw_row_but_eaten_cooked:
        portion_grams = raw_equivalent(grams, food.yield_factor)
        if food.yield_factor:
            steps.append(
                f"{grams:.0f}g cooked / yield {food.yield_factor:g} "
                f"= {portion_grams:.0f}g raw equivalent"
            )
        else:
            steps.append(f"{grams:.0f}g cooked, no yield factor on the row: used as-is")

    macros = per_100g(food).scaled(portion_grams / 100.0)
    steps.append(
        f"{portion_grams:.0f}g x {food.kcal_per_100g:g} kcal/100g = {macros.kcal:.0f} kcal"
    )

    if apply_fat_absorption:
        oil_g = absorbed_oil_grams(grams, food.fat_absorption_pct)
        if oil_g > 0:
            oil = Macros(
                kcal=OIL_KCAL_PER_100G * oil_g / 100.0,
                fat_g=OIL_FAT_G_PER_100G * oil_g / 100.0,
            )
            macros = macros + oil
            steps.append(
                f"+{oil_g:.1f}g absorbed oil ({food.fat_absorption_pct:g}%) = +{oil.kcal:.0f} kcal"
            )

    return Computation(macros=macros, grams_of_row=portion_grams, steps=tuple(steps))


def total(computations: list[Computation]) -> Macros:
    """Sum a meal. Empty means zero, not an error: a logged glass of water is a
    legitimate entry with no macros."""
    out = ZERO
    for c in computations:
        out = out + c.macros
    return out


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecipeProfile:
    """A recipe's per-100g composition, derived from its ingredients.

    Never typed in. Fix an ingredient row and every recipe containing it
    corrects itself, retroactively.
    """

    kcal_per_100g: float
    protein_g_per_100g: float
    carbs_g_per_100g: float
    fat_g_per_100g: float
    fiber_g_per_100g: float | None
    density_g_per_ml: float | None = None
    yield_factor: float | None = None
    fat_absorption_pct: float | None = None
    canonical_name_en: str = "recipe"


def recipe_profile(
    ingredients: list[tuple[FoodLike, float]],
    *,
    cooked_output_grams: float | None = None,
) -> RecipeProfile:
    """Per-100g profile of a finished dish.

    `ingredients` is (food row, grams as weighed) pairs. `cooked_output_grams`
    is the finished dish on the scale, which is what captures evaporation: put
    1400g in the pot, get 1100g of soup out, and a per-100g figure computed
    against the raw total would understate every serving by 27%.

    Falls back to the ingredient total when the dish was not weighed, which is
    correct for anything uncooked and wrong for anything simmered. The bot asks
    for the weight precisely so this fallback is rare.
    """
    if not ingredients:
        raise ValueError("a recipe needs at least one ingredient")

    raw_total = sum(g for _, g in ingredients)
    if raw_total <= 0:
        raise ValueError("recipe ingredients weigh nothing")

    macros = ZERO
    for food, grams in ingredients:
        macros = macros + compute(food, grams=grams).macros

    basis = cooked_output_grams if cooked_output_grams and cooked_output_grams > 0 else raw_total
    per_100 = macros.scaled(100.0 / basis)

    return RecipeProfile(
        kcal_per_100g=per_100.kcal,
        protein_g_per_100g=per_100.protein_g,
        carbs_g_per_100g=per_100.carbs_g,
        fat_g_per_100g=per_100.fat_g,
        fiber_g_per_100g=per_100.fiber_g,
        yield_factor=(cooked_output_grams / raw_total) if cooked_output_grams else None,
    )


def serving(profile: RecipeProfile, grams: float) -> Computation:
    """A portion of a finished recipe. Tier 3: better than any photo estimate."""
    return compute(profile, grams=grams)
