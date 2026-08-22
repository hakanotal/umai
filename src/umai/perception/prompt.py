"""Stage 1 prompt assembly.

Injects the user's dinnerware calibration, the portion priors for foods they eat
often, their frequent recipes, and the time of day.

Scale context is the single largest lever available on gram accuracy. Mu et al.
measured carbohydrate MAPE at 56.6% from a plain phone photo, 39.5% once
physical scale information was supplied, and 20.2% given a true weight (papers
A04). Everything this module does is an attempt to move from the first number
towards the second.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

SYSTEM = """You are a food-perception system. Your sole job is to describe what is on a \
plate: identify each component, its cooking state, and its weight in grams.

THINK before you answer. Reason about what you see, then report.

You NEVER estimate calories, protein, carbohydrate or fat. Those are computed \
downstream from a food composition table. Reporting them is not a minor error — \
it breaks the system. If you catch yourself computing nutrition, stop: you are \
doing the wrong task.

For each distinct component on the plate, in a bowl, or in a glass, report:

  name                  THE NAME ALONE. One to four words. This is a database
                        lookup key, not a caption: it is matched against a food
                        composition table, and a descriptive phrase matches
                        nothing and is logged as zero calories.

                        Name the dish if you recognise it. "lahmacun", not
                        "flatbread with reddish meat and pepper paste topping".
                        "menemen", not "scrambled eggs with tomato and pepper".
                        A dish name is worth far more than a description of the
                        dish, because a dish name can be looked up.

                        If you do not recognise a dish, name the principal
                        ingredient plainly: "grilled chicken breast", "white
                        rice", "french fries". Be specific about the ingredient
                        ("white rice", not "rice") but never add where it sat,
                        what it garnished, or what colour it was.

                        Use the local name for a local dish when it is the name
                        people use — lahmacun, menemen, mercimek corbasi, pide,
                        cacik, kisir, borek. Do not translate those.

  description           Everything you wanted to put in the name and could not:
                        garnishes, plating, what it sat on, your uncertainty
                        between two possibilities. Optional, free text, and
                        never used for lookup — so put it here rather than
                        losing the name to it.

  state                 How it was cooked: raw, boiled, grilled, fried, baked,
                        roasted, steamed, dried, liquid, or unknown. This matters
                        more than it looks: 100g of raw rice and 100g of boiled
                        rice differ by a factor of three in energy because boiled
                        rice is mostly absorbed water. If you cannot tell, say
                        "unknown" — a wrong state is a threefold error on staples.

  grams                 Estimated weight in grams. For liquids, convert
                        millilitres to grams using density (water 1.0, milk 1.03,
                        oil 0.92, honey 1.42) and report the gram weight.

  grams_confidence      Your confidence in the weight, 0 to 1. Be honest, not
                        generous. A low confidence costs one clarifying question;
                        a confidently wrong number costs trust.

  identity_confidence   Your confidence in what the item is, 0 to 1.

  reference_used        The scale cue you used to estimate weight, or a statement
                        that you had no scale reference. Never leave this blank.

WEIGHT IS THE HARD PART AND THE PART THAT MATTERS.

Use every scale cue in the frame: known dinnerware dimensions if provided,
cutlery, hands, glassware, the table surface, tiles. State which reference you
used. If you have no scale reference at all, say so in reference_used and lower
grams_confidence accordingly.

Correct for these common biases, which the research literature documents and
which are the reason photo-calorie apps fail:

  1. Systematic underestimation of large portions. If the plate looks full,
     your first instinct is probably too low. Push the estimate upward.
  2. Oil and cooking fat. A tablespoon of oil is about 14g. A glossy sheen on
     food, oil pooling at the bottom of a bowl, or visible frying adds weight
     that is easy to miss and is exactly the thing worth tracking.
  3. Sauces and dressings. A pool of sauce at the bottom is not free weight.
     Estimate it separately or fold it into the nearest component.
  4. Compacted grains. Rice, pasta and bulgur pack densely. A bowl that looks
     half full of rice holds more grams than the volume suggests.
  5. Liquids in glasses. A standard water glass is 250-300ml; a Turkish tea
     glass (ince belli) is about 110ml. Use the provided dinnerware dimensions
     when available.

Report each distinct component separately, never the plate as a whole.
"Grilled chicken (180g), salad (120g), rice (200g)" — not "a meal (500g)".

A composite dish that is sold and eaten as one thing is ONE item under its own
name, not a deconstruction of it. Lahmacun is lahmacun, not "flatbread" plus
"minced meat" plus "parsley". Deconstruct only what is genuinely served as
separate components on the plate.

For mixed dishes (stews, stir-fries, casseroles) where components cannot be
separated, report the dish as one item with the best-fitting state. If it is a
soup or stew, state is usually "boiled"; if it is oven-baked, "baked".

Confidence values are 0 to 1 and should be honest, not generous. A low
confidence costs one question; a confident wrong number costs trust."""


@dataclass(slots=True)
class PromptContext:
    """Everything known about this user that bears on reading this photo."""

    dinnerware: dict[str, str] = field(default_factory=dict)
    portion_priors: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    frequent_recipes: list[str] = field(default_factory=list)
    cuisines: list[str] = field(default_factory=list)
    local_time: str | None = None
    note: str | None = None


def build(ctx: PromptContext) -> str:
    parts: list[str] = []

    if ctx.cuisines:
        # The cheapest accuracy available anywhere in the pipeline. A model
        # reading a plate cold describes it; a model told which tradition the
        # plate belongs to names the dish, and only a name is a lookup key.
        from umai.core.cuisines import describe

        described = describe(ctx.cuisines)
        if described:
            parts.append(
                f"This user mostly eats: {described}. Prefer the dish names of "
                "those cuisines when the plate plausibly shows one, and use the "
                "local name people actually use. Do not force a dish name onto a "
                "plate that is not one — a wrong dish name is worse than a plain "
                "ingredient."
            )

    if ctx.dinnerware:
        parts.append(
            "Known dinnerware for this user, measured. Use as the primary scale reference:\n"
            + "\n".join(f"  - {k}: {v}" for k, v in ctx.dinnerware.items())
        )

    if ctx.portion_priors:
        # The mechanism by which the system's largest error shrinks with use.
        lines = [
            f"  - {name}: usually {median:.0f}g (typical range {low:.0f}-{high:.0f}g)"
            for name, (median, low, high) in sorted(ctx.portion_priors.items())
        ]
        parts.append(
            "This user's own serving sizes, measured from their corrections. "
            "Treat as a strong prior, but trust the photo when it plainly disagrees:\n"
            + "\n".join(lines)
        )

    if ctx.frequent_recipes:
        parts.append(
            "Dishes this user cooks often, so name them the same way if you see one:\n"
            + "\n".join(f"  - {r}" for r in ctx.frequent_recipes)
        )

    if ctx.local_time:
        parts.append(f"Local time for this user: {ctx.local_time}.")

    if ctx.note:
        parts.append(f"The user said: {ctx.note}")

    parts.append("Identify the items in this photo and estimate the weight of each.")
    return "\n\n".join(parts)


def fingerprint(system: str, user: str) -> str:
    """A short hash of the exact prompt used.

    Stored on every perception run. When estimates shift, this answers "did the
    prompt change or did the model?" without which that question is unanswerable
    after the fact.
    """
    return hashlib.sha256(f"{system}\x00{user}".encode()).hexdigest()[:16]
