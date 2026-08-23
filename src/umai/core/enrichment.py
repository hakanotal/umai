"""The background job that fills the holes in the food table.

The first live session made the case for this module better than any argument
could. Twelve of nineteen logged items resolved to nothing, were written with
zero macros, and were reported to the user as fact: a photo of lahmacun with
onion and parsley came back as "Total 10 kcal", the onion being the only thing
the 46-row starter table could match. A food table that only ever grows by hand
means the honest-but-useless zero is the *normal* case for months.

So: when the resolver cannot match an item, the gap is remembered. A job runs
in the background, off the critical path, and asks the largest model in the
roster what is in 100g of the thing — with the user's cuisines as context,
because "pide" is a Turkish flatbread and not an Italian one, and because the
model that knows lahmacun is minced lamb on thin dough is the same model that
knows it is about 250 kcal per 100g. The answer is validated in code, written
as a `foods` row, and the food_items that were waiting on it are backfilled.

The model does not have to answer from memory. It holds one tool, a trigram
search over `fndds_foods` — 5,431 rows of measured USDA composition — and
answers by one of three methods, in descending order of how much we trust it:

  * **copy**: a row in that table IS this food. The model names an fdc_id and
    nothing else; code reads the numbers out of the table. Tier 2, because the
    numbers are USDA's, and `source=usda_sr` because that is already what
    importers/usda.py assigns to Survey (FNDDS) data.
  * **compose**: no row is the dish, but rows are its ingredients. The model
    names fdc_ids and the percentage each contributes; code does the weighted
    arithmetic. Tier 4 — the numbers are measured but the proportions are not.
  * **recall**: the table has neither. Today's original path, unchanged, tier 4.

That table is American and English, which bounds what the tool is for: English
food names and specific ingredients. It has no lahmacun and no ayran, and the
0.30 similarity floor in core/fndds.py means a search for one returns nothing
rather than something. So for this user the lookup mostly pays off one
ingredient at a time, which is why the model is told to search them singly.

Four properties this module is arranged to keep, in order of importance.

**Nothing the model invents is trusted.** A recalled composition passes
`validate`, whose central check is Atwater: 4·protein + 4·carbs + 9·fat must
reconstruct the stated energy. That single arithmetic identity catches the
great majority of plausible-sounding invention, because a model that is
confabulating a composition rarely confabulates one that is internally
consistent. A rejected candidate leaves a row in `enrichment_attempts` saying
why.

**...but that check is not applied to measured numbers, deliberately.** 40 of
the 5,431 FNDDS rows fail it outright, every one of them explicably: spirits
are ~231 kcal of ethanol with no macros at all, which 4/4/9 cannot see; cocoa
powder carries 37g of fibre inside carbohydrate-by-difference, which yields ~2
kcal/g rather than 4; stevia powder is 100g of polyol declared as zero energy.
Gating a copied row on Atwater would throw out real USDA data for all forty.
The gate belongs to the recall path alone. See `validate`.

**The model chooses; code copies.** On the lookup paths the model never states
a number, only an fdc_id — and only an fdc_id that a search in this same
conversation actually returned, which `validate` enforces against the `offered`
set. This is the division resolver/match.py already keeps for its tiebreak:
the model does judgement, never arithmetic.

**Only a verbatim row beats tier 4.** Tier 4 is the plan's "model-invented,
provisional, never silently fact" tier: the calibration engine excludes windows
these dominate, and the bot marks them when it shows them. Composition and
recall both stay there. Tier 2 is reserved for numbers that are USDA's own,
copied byte for byte, with the fdc_id recorded in `source_ref` so the claim can
be audited later.

**It never runs on the critical path.** The user's reply must not wait on the
largest model in the roster researching a side salad. The job is scheduled, and
a fresh gap only *nudges* it; the nudge is fire-and-forget.

Macros on `food_items` are a cache, always recomputable from `food_id` plus
grams — which is precisely what makes the backfill legitimate rather than a
violation of entry immutability. No `log_entries` row is touched, no gram value
changes, and the arithmetic is the same code the original write used.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from umai.clock import Clock
from umai.config.models import ModelClient
from umai.core.cuisines import describe
from umai.db.models import (
    EnrichmentAttempt,
    EntryKind,
    FnddsFood,
    Food,
    FoodItem,
    FoodSource,
    FoodState,
    LogEntry,
    ResolutionMethod,
    User,
)

log = logging.getLogger(__name__)

# Give up after this many rejected attempts at one name. A dish the model
# cannot describe consistently three times running is not going to become
# describable on the fourth, and each attempt costs a call to the most
# expensive model configured.
MAX_ATTEMPTS = 3

# How many gaps one tick researches. Small on purpose: the job runs often, the
# work is not urgent, and a burst of twenty calls to the coach tier is a
# noticeable line on a monthly bill that is supposed to read $1.36.
BATCH = 4

# Postgres advisory lock key, so two processes (or a scheduled tick overlapping
# a nudge) cannot research the same gaps concurrently. Arbitrary constant;
# the only requirement is that nothing else in the database uses it.
LOCK_KEY = 0x756D_6169_0001


# ---------------------------------------------------------------------------
# What the model is asked, and what it is allowed to come back with
# ---------------------------------------------------------------------------

SYSTEM = """You are a food composition reference. You are given the name of a dish or \
ingredient that is missing from a nutrition database, and you return its \
composition per 100 grams as eaten.

You have one tool, fndds_search, over the USDA FNDDS food composition table. Its \
numbers are measured; yours are remembered. Prefer measured numbers.

WHAT THAT TABLE IS. It is American and its entries are in English: "Soup, \
lentil", "Beef, ground", "Cheese, Feta", "Chicken fillet, grilled". Search it \
for English food names and for specific ingredients. Do NOT search it for a \
dish named in another language — it holds no lahmacun, no ayran, no simit, and \
a search for one is not a near miss, it is the wrong table. Do not try \
translations or paraphrases of a dish name to coax a match; if the dish itself \
is not an English-named food, go straight to its ingredients or to recall.

Search ingredients ONE AT A TIME, one search per ingredient: "ground lamb", \
then "onion", then "tomato". A search carrying several ingredients at once \
matches none of them.

Choose one of three methods and set "method" accordingly:

  * "copy" — a row you found IS this food. Give its fdc_id and nothing else;
    the composition is copied from the table, so do not restate the numbers.
    This is the best outcome and you should reach for it first whenever the
    item is an English food name or a plain ingredient.
  * "compose" — no single row is this food, but you found rows for its
    ingredients. Give the fdc_ids with the percentage each contributes to 100g
    of the dish as eaten. Percentages must sum to about 100. The arithmetic is
    done for you from the measured rows; supply only proportions.
  * "recall" — the table does not have this food or its ingredients. Report the
    composition from your own knowledge, as below.

A returned row is a candidate, not an answer. The search ranks by spelling, not \
by meaning, so it will hand you "Bread, onion" for "onion" and "Coffee, \
Turkish" for "turkish tea". Read every candidate and reject the whole set if \
none of them is genuinely the same food — searching again with a better word, \
or falling back to "recall", is always better than copying a row that is not \
the food.

Rules that decide whether your answer is accepted:

  * Report per 100 GRAMS OF THE FOOD AS EATEN, in the cooked state given. Not
    per portion, not per piece, not per raw ingredient.
  * For "recall" your numbers must be arithmetically consistent. Energy is
    checked against 4*protein + 4*carbs + 9*fat, and an answer that does not
    reconcile is discarded. Check it yourself before answering. (Rows you copy
    or compose are exempt: they are measured, and measured food does not always
    obey those factors.)
  * canonical_name_en is a short lookup key, 1-4 words. Put the local name in
    aliases, not in the name. It names the food you were asked about, never the
    FNDDS row you copied — asked for "kiymali pide", canonical_name_en is
    "kiymali pide" even if you composed it from beef and flatbread rows.
  * aliases should include the local-language name, the English name, and the
    common alternative spellings. This is how the same dish resolves next time
    whichever language it is typed in.
  * ALWAYS return is_food. Set it true for anything edible, including a bare
    ingredient name or a garnish. Set it false only for something that is not
    food at all — a plate, a piece of cutlery, a description of where something
    sat, something you genuinely cannot identify. Returning a guess for a
    non-food is the worst outcome available to you; refusing an ordinary
    ingredient because its name is terse is the second worst.
  * If you do not know this specific dish, say so with a low confidence rather
    than inventing. A low confidence is useful; a confident invention is not.

yield_factor is cooked weight divided by raw weight, and belongs on a row that \
describes a RAW ingredient (boiled rice ~2.6, roast meat ~0.75). Leave it null \
for a row that already describes the cooked dish. fat_absorption_pct is the oil \
a food takes up when deep fried, as a percentage of its weight, and is likewise \
null unless the row describes the unfried food."""

# The one tool the enrichment model is given. Described in the terms the model
# needs to use it well: what the table contains, and what a miss means.
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "fndds_search",
            "description": (
                "Search the USDA FNDDS food composition table by name. The table is "
                "American and its entries are English, so search English food names "
                "and specific ingredients, one ingredient per call. Returns up to 5 "
                "candidate rows with their fdc_id and measured composition per 100g, "
                "best match first, or an empty list when nothing in the table is "
                "close — which is the normal answer for a dish named in another "
                "language, and means you should search an ingredient instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "An English food or ingredient name, e.g. 'ground beef', "
                            "'lentil soup', 'feta cheese'."
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    }
]

# Each turn is a paid call to the largest model in the roster, and the job runs
# every 20 minutes. Four is enough for a search, a correction and an answer;
# a model still hunting on the fifth is not converging.
MAX_TOOL_TURNS = 4

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_food": {"type": "boolean"},
        "method": {"type": "string", "enum": ["copy", "compose", "recall"]},
        # method=copy: the row that IS this food.
        "fdc_id": {"type": ["integer", "null"]},
        # method=compose: the rows its ingredients are, and their shares of 100g.
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fdc_id": {"type": "integer"},
                    "pct": {"type": "number"},
                },
                "required": ["fdc_id", "pct"],
            },
        },
        "canonical_name_en": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "state": {"type": "string"},
        # method=recall only. Ignored for copy and compose, where the numbers
        # come from the table.
        "kcal_per_100g": {"type": ["number", "null"]},
        "protein_g_per_100g": {"type": ["number", "null"]},
        "carbs_g_per_100g": {"type": ["number", "null"]},
        "fat_g_per_100g": {"type": ["number", "null"]},
        "fiber_g_per_100g": {"type": ["number", "null"]},
        "density_g_per_ml": {"type": ["number", "null"]},
        "yield_factor": {"type": ["number", "null"]},
        "fat_absorption_pct": {"type": ["number", "null"]},
        "confidence": {"type": "number"},
        "basis": {"type": "string"},
    },
    "required": [
        "is_food",
        "method",
        "canonical_name_en",
        "aliases",
        "state",
        "confidence",
        "basis",
    ],
}


# --- validation, the part that matters -------------------------------------

# Nothing eaten is denser in energy than pure fat. 920 rather than 900 for one
# concrete reason: USDA's own FNDDS row for lard is 902 kcal/100g, and every
# seed oil sits at exactly 900, so a ceiling of 900 rejects measured data at
# the boundary. It is the only row of 5,431 that exceeds it, but rejecting a
# real USDA row as a "units error" is the wrong failure. The gate is here to
# catch a units error or an invention, both of which are wrong by a factor of
# ten, not by two per cent.
MAX_KCAL_PER_100G = 920.0
# Below this and it is water with a flavour, which is possible (tea, broth) but
# is also what a model returns when it has no idea. Allowed, but it must still
# reconcile.
MIN_CONFIDENCE = 0.35

# Atwater tolerance. Generous on purpose: fibre is counted differently across
# references, sugar alcohols and organic acids contribute, and published rows
# are rounded. Tight enough that an invented composition rarely survives.
ATWATER_ABS_TOLERANCE = 60.0
ATWATER_REL_TOLERANCE = 0.30


@dataclass(slots=True)
class Candidate:
    canonical_name_en: str
    aliases: list[str]
    state: FoodState
    kcal_per_100g: float
    protein_g_per_100g: float
    carbs_g_per_100g: float
    fat_g_per_100g: float
    fiber_g_per_100g: float | None
    density_g_per_ml: float | None
    yield_factor: float | None
    fat_absorption_pct: float | None
    confidence: float
    basis: str
    # Where the numbers came from, which is what decides the trust tier. Not the
    # model's word for it: `method` is set by the validator after it has checked
    # that a claimed fdc_id was really offered and really resolved.
    method: str = "recall"
    source: FoodSource = FoodSource.model
    source_ref: str = ""
    trust_tier: int = 4


class Rejected(ValueError):
    """The candidate did not survive validation. The message is stored."""


def atwater_kcal(protein: float, carbs: float, fat: float) -> float:
    """Energy implied by the macros. The identity every real food obeys."""
    return 4.0 * protein + 4.0 * carbs + 9.0 * fat


# Weights that miss 100 by more than this are not a composition, they are a
# guess with arithmetic attached. Within it, they are normalised: a model that
# says 45/30/12/8/5 has described a whole dish and the 100 is rounding.
COMPOSE_PCT_TOLERANCE = 5.0

# The largest number of ingredient rows a composed food may draw on. Beyond
# this the proportions carry more error than the measured rows remove.
MAX_COMPONENTS = 8


def _compose(components: list[tuple[FnddsFood, float]]) -> dict[str, float]:
    """Weighted per-100g composition of a dish built from measured rows.

    Weights are normalised by their own sum rather than assumed to be 100, so
    the result is per 100g of the dish however the model rounded. This is the
    arithmetic the model is not allowed to do: it supplies proportions, code
    supplies numbers.
    """
    total = sum(pct for _row, pct in components)
    out = {
        "kcal_per_100g": 0.0,
        "protein_g_per_100g": 0.0,
        "carbs_g_per_100g": 0.0,
        "fat_g_per_100g": 0.0,
        "fiber_g_per_100g": 0.0,
    }
    for row, pct in components:
        w = pct / total
        out["kcal_per_100g"] += w * row.kcal_per_100g
        out["protein_g_per_100g"] += w * row.protein_g_per_100g
        out["carbs_g_per_100g"] += w * row.carbs_g_per_100g
        out["fat_g_per_100g"] += w * row.fat_g_per_100g
        out["fiber_g_per_100g"] += w * (row.fiber_g_per_100g or 0.0)
    return out


def validate(
    raw: dict[str, Any],
    *,
    expected_state: FoodState,
    offered: dict[int, FnddsFood] | None = None,
) -> Candidate:
    """Turn a model response into a row, or raise Rejected saying why.

    Every gate here is arithmetic or a range. None of them asks the model
    whether it is confident in itself except as a floor, because a model's
    stated confidence is the least reliable number it returns.

    `offered` holds every FNDDS row the searches in this session actually
    returned, keyed by fdc_id. It is the guard that makes the lookup path
    trustworthy: a model may only copy from or compose out of rows it was really
    shown, so an fdc_id recalled from training data — or invented outright, in
    the shape of a plausible eight-digit number — is rejected rather than
    silently read out of the reference table.

    **The Atwater gate applies to recalled numbers only, and that is deliberate.**
    It exists to catch invention: a model confabulating a composition rarely
    confabulates an internally consistent one. Measured food is under no such
    obligation. 40 of the 5,431 FNDDS rows fail this exact check — whiskey and
    the other spirits, which are ~231 kcal of ethanol the factors cannot see;
    cocktails; cocoa powder, whose 37g of fibre sits inside
    carbohydrate-by-difference at ~2 kcal/g rather than 4; and the polyol
    sweeteners, 100g of carbohydrate declared as zero energy. Applying the gate
    to a copied row would reject genuine USDA data for every one of them. So:
    gate what the model made up, trust what USDA measured, and never let the
    two paths share a check that only suits one of them.
    """
    # Only an *explicit* denial counts. The enrichment model runs without a
    # strict schema (glm-5.3 has no structured outputs), so a field can simply
    # be absent, and treating absence as "not a food" rejected "french fries"
    # and "brisket" on one run and accepted them on the next. The arithmetic
    # gates below are the real guard; this one catches the model actively
    # refusing, which is what it is for.
    if raw.get("is_food") is False:
        raise Rejected("model says this is not a food")

    name = str(raw.get("canonical_name_en") or "").strip().lower()
    if not name:
        raise Rejected("no canonical name")
    if len(name) > 200:
        raise Rejected(f"name is {len(name)} characters, not a lookup key")

    def _num(key: str, default: float | None = None) -> float | None:
        value = raw.get(key, default)
        if value is None:
            return None
        try:
            out = float(value)
        except (TypeError, ValueError) as exc:
            raise Rejected(f"{key} is not a number: {value!r}") from exc
        if out != out or out in (float("inf"), float("-inf")):
            raise Rejected(f"{key} is not finite")
        return out

    available = offered or {}
    method = str(raw.get("method") or "recall").strip().lower()
    if method not in ("copy", "compose", "recall"):
        raise Rejected(f"unknown method {method!r}")

    source = FoodSource.model
    trust_tier = 4
    basis = str(raw.get("basis") or "")[:500]

    if method == "copy":
        # The model's entire contribution here is the choice of row. It states
        # an fdc_id; the numbers are read from the table.
        try:
            fdc_id = int(raw["fdc_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise Rejected(f"method=copy without a usable fdc_id: {raw.get('fdc_id')!r}") from exc
        row = available.get(fdc_id)
        if row is None:
            raise Rejected(f"fdc_id {fdc_id} was never returned by a search in this session")
        kcal = row.kcal_per_100g
        protein = row.protein_g_per_100g
        carbs = row.carbs_g_per_100g
        fat = row.fat_g_per_100g
        fiber = row.fiber_g_per_100g
        # Tier 2 and usda_sr are what importers/usda.py already assigns to
        # "Survey (FNDDS)" data. Same data, same provenance, same tier.
        source = FoodSource.usda_sr
        trust_tier = 2
        source_ref = f"fndds:{fdc_id} {row.description}"[:200]
        basis = f"copied FNDDS {fdc_id} ({row.description}). {basis}"[:500]

    elif method == "compose":
        rows_pct: list[tuple[FnddsFood, float]] = []
        seen: set[int] = set()
        components = raw.get("components") or []
        if not isinstance(components, list) or not components:
            raise Rejected("method=compose with no components")
        if len(components) > MAX_COMPONENTS:
            raise Rejected(f"{len(components)} components is more than {MAX_COMPONENTS}")
        for entry in components:
            if not isinstance(entry, dict):
                raise Rejected(f"component is not an object: {entry!r}")
            try:
                cid = int(entry["fdc_id"])
                pct = float(entry["pct"])
            except (KeyError, TypeError, ValueError) as exc:
                raise Rejected(f"unusable component {entry!r}") from exc
            if pct <= 0:
                raise Rejected(f"component {cid} has non-positive share {pct}")
            crow = available.get(cid)
            if crow is None:
                raise Rejected(f"fdc_id {cid} was never returned by a search in this session")
            if cid in seen:
                raise Rejected(f"fdc_id {cid} listed twice")
            seen.add(cid)
            rows_pct.append((crow, pct))

        total_pct = sum(pct for _r, pct in rows_pct)
        if abs(total_pct - 100.0) > COMPOSE_PCT_TOLERANCE:
            raise Rejected(
                f"component shares sum to {total_pct:.0f}%, not 100% "
                f"(tolerance {COMPOSE_PCT_TOLERANCE:.0f})"
            )
        composed = _compose(rows_pct)
        kcal = composed["kcal_per_100g"]
        protein = composed["protein_g_per_100g"]
        carbs = composed["carbs_g_per_100g"]
        fat = composed["fat_g_per_100g"]
        fiber = composed["fiber_g_per_100g"]
        # Stays tier 4: the numbers are measured but the proportions are the
        # model's judgement, and a wrong proportion is as wrong as a wrong
        # number. Only a verbatim row earns tier 2.
        parts = ", ".join(f"{pct:.0f}% fdc {row.fdc_id}" for row, pct in rows_pct)
        source_ref = f"fndds-composed: {parts}"[:200]
        basis = f"composed from FNDDS ({parts}). {basis}"[:500]

    else:
        kcal_opt = _num("kcal_per_100g")
        if kcal_opt is None:
            raise Rejected("method=recall without kcal_per_100g")
        kcal = kcal_opt
        protein = _num("protein_g_per_100g", 0.0) or 0.0
        carbs = _num("carbs_g_per_100g", 0.0) or 0.0
        fat = _num("fat_g_per_100g", 0.0) or 0.0
        fiber = _num("fiber_g_per_100g")
        source_ref = f"enrichment: {basis[:150]}"

    for label, value in (("kcal", kcal), ("protein", protein), ("carbs", carbs), ("fat", fat)):
        if value < 0:
            raise Rejected(f"{label} is negative")
    if kcal > MAX_KCAL_PER_100G:
        raise Rejected(f"{kcal:.0f} kcal/100g exceeds pure fat; units error or invention")
    if protein + carbs + fat > 105.0:
        raise Rejected(
            f"macros sum to {protein + carbs + fat:.0f}g in 100g of food, which is impossible"
        )

    # See the docstring: invention has to reconcile, measurement does not.
    if method == "recall":
        implied = atwater_kcal(protein, carbs, fat)
        tolerance = max(ATWATER_ABS_TOLERANCE, ATWATER_REL_TOLERANCE * kcal)
        if abs(implied - kcal) > tolerance:
            raise Rejected(
                f"Atwater check failed: macros imply {implied:.0f} kcal, "
                f"row states {kcal:.0f} kcal (tolerance {tolerance:.0f})"
            )

    confidence = _num("confidence", 0.0) or 0.0
    if confidence < MIN_CONFIDENCE:
        raise Rejected(f"model confidence {confidence:.2f} below floor")

    state_raw = str(raw.get("state", "")).strip().lower()
    state = FoodState(state_raw) if state_raw in FoodState._value2member_map_ else expected_state

    yield_factor = _num("yield_factor")
    if yield_factor is not None and not (0.1 < yield_factor < 5.0):
        # The foods table has a check constraint here; catching it now gives a
        # readable reason instead of an IntegrityError at flush.
        yield_factor = None
    absorption = _num("fat_absorption_pct")
    if absorption is not None and not (0.0 <= absorption <= 40.0):
        absorption = None
    density = _num("density_g_per_ml")
    if density is not None and not (0.5 < density < 2.0):
        density = None

    aliases = []
    for alias in raw.get("aliases") or []:
        cleaned = " ".join(str(alias).strip().lower().split())
        if cleaned and cleaned != name and cleaned not in aliases and len(cleaned) <= 200:
            aliases.append(cleaned)

    return Candidate(
        canonical_name_en=name,
        aliases=aliases[:12],
        state=state,
        kcal_per_100g=kcal,
        protein_g_per_100g=protein,
        carbs_g_per_100g=carbs,
        fat_g_per_100g=fat,
        fiber_g_per_100g=fiber,
        density_g_per_ml=density,
        yield_factor=yield_factor,
        fat_absorption_pct=absorption,
        confidence=confidence,
        basis=basis,
        method=method,
        source=source,
        source_ref=source_ref,
        trust_tier=trust_tier,
    )


# ---------------------------------------------------------------------------
# Finding the gaps
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Gap:
    detected_name: str
    state: FoodState
    occurrences: int


def _gaps_stmt(limit: int) -> Select[Any]:
    """Unmatched items, most frequently logged first.

    Frequency ordering is the whole scheduling policy: the thing you eat every
    week is worth a call to the expensive model, and the one-off side salad
    from a restaurant in March can wait behind it forever without harm.
    """
    exhausted = select(EnrichmentAttempt.detected_name).where(
        EnrichmentAttempt.attempts >= MAX_ATTEMPTS,
        EnrichmentAttempt.food_id.is_(None),
    )
    return (
        select(
            func.lower(FoodItem.detected_name).label("detected_name"),
            FoodItem.detected_state,
            func.count().label("occurrences"),
        )
        .join(LogEntry, FoodItem.entry_id == LogEntry.id)
        .where(
            FoodItem.food_id.is_(None),
            FoodItem.recipe_id.is_(None),
            FoodItem.detected_name.is_not(None),
            func.length(func.trim(FoodItem.detected_name)) > 1,
            LogEntry.superseded_by.is_(None),
            LogEntry.kind.in_((EntryKind.food, EntryKind.drink)),
            FoodItem.detected_name.not_in(exhausted),
        )
        .group_by(func.lower(FoodItem.detected_name), FoodItem.detected_state)
        .order_by(func.count().desc(), func.lower(FoodItem.detected_name))
        .limit(limit)
    )


async def pending_gaps(session: AsyncSession, limit: int = BATCH) -> list[Gap]:
    rows = (await session.execute(_gaps_stmt(limit))).all()
    return [
        Gap(
            detected_name=r.detected_name,
            state=r.detected_state or FoodState.unknown,
            occurrences=int(r.occurrences),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Researching one gap
# ---------------------------------------------------------------------------


async def _lookup(
    session_factory: async_sessionmaker[AsyncSession],
    query: str,
    offered: dict[int, FnddsFood],
) -> list[dict[str, object]]:
    """Run one FNDDS search, remembering every row it showed the model.

    Each search gets its own short session. The architecture forbids holding a
    transaction across a model call, and this runs between two of them.

    `offered` accumulates across the whole conversation and is what `validate`
    checks an fdc_id against, so a row the model never actually saw cannot be
    copied from.
    """
    from umai.core import fndds

    async with session_factory() as session:
        hits = await fndds.search(session, query)
        rows = await fndds.by_ids(session, [h.fdc_id for h in hits])
    offered.update(rows)
    return [h.as_tool_result() for h in hits]


async def research(
    models: ModelClient,
    gap: Gap,
    cuisines: list[str],
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Candidate, str]:
    """Ask the largest model what is in this. Returns (candidate, model served).

    A bounded tool-calling loop rather than one shot: the model may search the
    FNDDS reference table, read what came back, and search again for an
    ingredient before answering. It stops when the model replies without a tool
    call, and gives up after MAX_TOOL_TURNS.

    The first search is run here, in code, on the item's own name. It is free —
    no model call — and for anything already named in English it puts the answer
    in front of the model before it has had to ask, which is the difference
    between one paid turn and three. For a name this table does not cover it
    returns nothing, and nothing is the correct and useful answer.

    Raises Rejected if the answer does not survive validation, and lets
    transport errors propagate to the caller, which records them.
    """
    context = describe(cuisines)
    hint = (
        f"This person mostly eats {context}. If the name belongs to one of "
        "those cuisines, treat it as that dish.\n\n"
        if context
        else ""
    )
    state_line = (
        f"It was observed in this state: {gap.state.value}.\n"
        if gap.state is not FoodState.unknown
        else ""
    )

    offered: dict[int, FnddsFood] = {}
    seeded = await _lookup(session_factory, gap.detected_name, offered)
    if seeded:
        opening = (
            f"I already searched FNDDS for that name; these are the matches:\n"
            f"{json.dumps(seeded)}\n\n"
            "If one of them is the food, copy it. If none is, search for its "
            "ingredients or answer from your own knowledge."
        )
    else:
        opening = (
            "I already searched FNDDS for that name and it returned nothing, "
            "which means the table does not hold that food under that name. "
            "Search for its individual ingredients in English, or answer from "
            "your own knowledge."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                f'{hint}{state_line}Give the composition per 100g of: "{gap.detected_name}"\n\n'
                f"{opening}"
            ),
        },
    ]

    from umai.perception.client import parse_json_object

    served = ""
    for turn in range(MAX_TOOL_TURNS):
        last = turn == MAX_TOOL_TURNS - 1
        if last:
            messages.append(
                {
                    "role": "system",
                    "content": "No further searches. Reply now with the final JSON object.",
                }
            )

        message, served, _ms = await models.acall_tools(
            "enrichment", messages=messages, tools=TOOLS, schema=SCHEMA
        )
        tool_calls = getattr(message, "tool_calls", None)
        if not tool_calls:
            raw = parse_json_object(message.content)
            return validate(raw, expected_state=gap.state, offered=offered), served

        # Rebuild the assistant turn explicitly rather than echoing the SDK
        # object back. OpenRouter decorates messages with provider-specific
        # fields (reasoning traces among them) that some providers reject on
        # the way back in.
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                query = str(args.get("query") or "")
            except json.JSONDecodeError:
                query = ""
            results = await _lookup(session_factory, query, offered) if query else []
            log.debug("enrichment fndds_search(%r) -> %d hit(s)", query, len(results))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(results),
                }
            )

    raise Rejected(f"no answer after {MAX_TOOL_TURNS} turns; still calling tools")


# ---------------------------------------------------------------------------
# Writing the row and backfilling what was waiting on it
# ---------------------------------------------------------------------------


async def upsert_food(session: AsyncSession, candidate: Candidate) -> Food:
    """Insert the row, or return (possibly upgrading) the existing name+state row.

    The name+state pair is unique in `foods`, and two gaps can normalise onto
    one name ("lahmacun" from a photo and from typed text), so the conflict is
    the expected case rather than an error.

    A better-sourced row may replace a worse one, and only in that direction:
    the existing row must be tier 4 and the incoming row strictly better. Before
    the FNDDS table existed every row this module wrote was recalled, so without
    the upgrade a dish researched last week would keep its guessed composition
    forever while the measured USDA row sat one query away. A tier-1 lab row or
    a tier-2 label is still never touched — the rule that a guess must not
    overwrite a measurement is intact, this is the same rule read forwards.
    """
    stmt = (
        insert(Food)
        .values(
            id=uuid.uuid4(),
            canonical_name_en=candidate.canonical_name_en,
            aliases=candidate.aliases,
            state=candidate.state,
            kcal_per_100g=candidate.kcal_per_100g,
            protein_g_per_100g=candidate.protein_g_per_100g,
            carbs_g_per_100g=candidate.carbs_g_per_100g,
            fat_g_per_100g=candidate.fat_g_per_100g,
            fiber_g_per_100g=candidate.fiber_g_per_100g,
            density_g_per_ml=candidate.density_g_per_ml,
            yield_factor=candidate.yield_factor,
            fat_absorption_pct=candidate.fat_absorption_pct,
            source=candidate.source,
            source_ref=candidate.source_ref[:200],
            trust_tier=candidate.trust_tier,
        )
        .on_conflict_do_nothing(constraint="uq_foods_name_state")
        .returning(Food.id)
    )
    food_id = (await session.execute(stmt)).scalar_one_or_none()
    if food_id is None:
        existing = (
            await session.execute(
                select(Food).where(
                    Food.canonical_name_en == candidate.canonical_name_en,
                    Food.state == candidate.state,
                )
            )
        ).scalar_one()
        if existing.trust_tier == 4 and candidate.trust_tier < existing.trust_tier:
            log.info(
                "enrichment: upgrading %r from tier %d to tier %d (%s)",
                existing.canonical_name_en,
                existing.trust_tier,
                candidate.trust_tier,
                candidate.source_ref,
            )
            existing.kcal_per_100g = candidate.kcal_per_100g
            existing.protein_g_per_100g = candidate.protein_g_per_100g
            existing.carbs_g_per_100g = candidate.carbs_g_per_100g
            existing.fat_g_per_100g = candidate.fat_g_per_100g
            existing.fiber_g_per_100g = candidate.fiber_g_per_100g
            existing.source = candidate.source
            existing.source_ref = candidate.source_ref[:200]
            existing.trust_tier = candidate.trust_tier
            # Aliases are merged, not replaced: the old row's may be the ones
            # the resolver has been matching on.
            merged = list(existing.aliases or [])
            for alias in candidate.aliases:
                if alias not in merged:
                    merged.append(alias)
            existing.aliases = merged[:12]
        return existing
    return (await session.execute(select(Food).where(Food.id == food_id))).scalar_one()


async def backfill(session: AsyncSession, gap: Gap, food: Food) -> int:
    """Point the waiting food_items at the new row and recompute their macros.

    Legitimate because macro columns are a cache: the architecture states they
    are always recomputable from food_id plus grams, and fixing a foods row is
    supposed to correct everything derived from it. Entries themselves are not
    touched — no gram value changes, nothing is superseded, and the arithmetic
    is the same `tools.macros_for_food` the original write used.

    Matches on name (case-insensitive) and state: different states have different
    compositions, so "boiled rice" and "raw rice" are separate gaps. Items
    detected as "unknown" are matched regardless of the gap's state, since an
    item logged as "unknown" that resolved to a baked row is exactly the case
    this exists to fix.
    """
    from umai.core.tools import macros_for_food

    items = (
        (
            await session.execute(
                select(FoodItem)
                .join(LogEntry, FoodItem.entry_id == LogEntry.id)
                .where(
                    FoodItem.food_id.is_(None),
                    FoodItem.recipe_id.is_(None),
                    func.lower(FoodItem.detected_name) == gap.detected_name.lower(),
                    or_(
                        FoodItem.detected_state == gap.state,
                        FoodItem.detected_state.is_(None),
                    ),
                    LogEntry.superseded_by.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    for item in items:
        macros = macros_for_food(
            food, grams=item.grams, detected_state=item.detected_state or FoodState.unknown
        )
        item.food_id = food.id
        item.resolution_method = ResolutionMethod.new
        # Provenance, expressed as confidence. A tier-4 row was matched by a
        # model that had been told the food table did not hold the food: the
        # number is now real, but its confidence is not high. A tier-2 row is a
        # USDA measurement the model merely pointed at, which deserves better —
        # though not as much as the resolver's own confident trigram match,
        # because the choice of row was still a judgement.
        item.resolution_confidence = 0.7 if food.trust_tier <= 2 else 0.5
        item.kcal = macros.kcal
        item.protein_g = macros.protein_g
        item.carbs_g = macros.carbs_g
        item.fat_g = macros.fat_g
        item.fiber_g = macros.fiber_g
    return len(items)


async def _record(
    session: AsyncSession,
    gap: Gap,
    clock: Clock,
    *,
    cuisines: list[str],
    food_id: uuid.UUID | None,
    error: str | None,
    model: str | None,
) -> None:
    """Remember the attempt, successful or not, so it is not repeated blindly."""
    now = clock.now()
    stmt = (
        insert(EnrichmentAttempt)
        .values(
            id=uuid.uuid4(),
            detected_name=gap.detected_name,
            state=gap.state,
            food_id=food_id,
            attempts=1,
            last_error=error,
            cuisines=cuisines,
            model=model,
            last_attempt_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_enrichment_name_state",
            set_={
                "attempts": EnrichmentAttempt.__table__.c.attempts + 1,
                "food_id": func.coalesce(
                    insert(EnrichmentAttempt).excluded.food_id,
                    EnrichmentAttempt.__table__.c.food_id,
                ),
                "last_error": error,
                "model": model,
                "cuisines": cuisines,
                "last_attempt_at": now,
            },
        )
    )
    await session.execute(stmt)


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EnrichmentReport:
    researched: int = 0
    created: int = 0
    backfilled_items: int = 0
    rejected: int = 0

    @property
    def did_work(self) -> bool:
        return bool(self.researched)


async def enrich_once(
    session_factory: async_sessionmaker[AsyncSession],
    models: ModelClient,
    clock: Clock,
    *,
    limit: int = BATCH,
) -> EnrichmentReport:
    """One tick. Safe to call concurrently; the loser of the lock does nothing.

    Each gap gets its own transaction. A model that returns nonsense for the
    third item must not roll back the two rows that were already good, and a
    long-running research call must not hold a write transaction open — which
    was already a bug on the photo path and is not worth reproducing here.
    """
    report = EnrichmentReport()

    async with session_factory() as session:
        got = (await session.execute(select(func.pg_try_advisory_lock(LOCK_KEY)))).scalar()
        if not got:
            log.debug("enrichment: another worker holds the lock")
            return report
        try:
            gaps = await pending_gaps(session, limit)
            cuisines = await _cuisines(session)
        finally:
            await session.execute(select(func.pg_advisory_unlock(LOCK_KEY)))
            await session.commit()

    for gap in gaps:
        candidate: Candidate | None = None
        served: str | None = None
        error: str | None = None
        try:
            candidate, served = await research(models, gap, cuisines, session_factory)
        except Rejected as exc:
            error = str(exc)
            report.rejected += 1
            log.info("enrichment rejected %r: %s", gap.detected_name, error)
        except Exception as exc:  # transport, parse, provider outage
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("enrichment failed for %r: %s", gap.detected_name, error)
        report.researched += 1

        async with session_factory() as session:
            food_id = None
            if candidate is not None:
                food = await upsert_food(session, candidate)
                food_id = food.id
                report.created += 1
                report.backfilled_items += await backfill(session, gap, food)
                log.info(
                    "enrichment: %r -> %s (%.0f kcal/100g), %d items backfilled",
                    gap.detected_name,
                    food.canonical_name_en,
                    food.kcal_per_100g,
                    report.backfilled_items,
                )
            await _record(
                session,
                gap,
                clock,
                cuisines=cuisines,
                food_id=food_id,
                error=error,
                model=served,
            )
            await session.commit()

    return report


async def _cuisines(session: AsyncSession) -> list[str]:
    """The single user's cuisines. Multi-user makes this per-gap, which needs
    the gap query to carry a user_id; not worth the join today."""
    user = (await session.execute(select(User).limit(1))).scalar_one_or_none()
    return list(user.cuisines or []) if user is not None else []


def summarise(report: EnrichmentReport) -> str:
    """One line for the log and, when it did something useful, for the chat."""
    return (
        f"researched {report.researched}, added {report.created} foods, "
        f"backfilled {report.backfilled_items} items, rejected {report.rejected}"
    )
