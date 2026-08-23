"""Intent classification and reply generation.

Context is bounded: today's entries, current targets and remaining budget,
trend weight and direction, recent corrections, calibration state, any active
commitment, and the time. Not the full history, which is reached through
query_history.

Phase 1 shape: one cheap structured call classifies and extracts from free
text; button taps never reach a model at all; every number shown back comes
out of core.tools, never out of the model. The coach tier (summaries with a
voice, weekly review) arrives in Phase 3 — until then replies are formatted in
code so there is exactly zero risk of an invented number.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from umai.analytics import safety
from umai.clock import Clock, local_date
from umai.config.models import ModelClient
from umai.core import cuisines as cuisines_mod
from umai.core import tools
from umai.db.models import (
    EntryKind,
    EntrySource,
    FoodState,
    GramsSource,
    Media,
    PerceptionRun,
    User,
)
from umai.perception.client import PerceptionOutcome, parse_json_object
from umai.resolver.match import Resolver

log = logging.getLogger(__name__)

INTENT_SYSTEM = """You route messages for a personal nutrition assistant. Reply with a \
single JSON object and nothing else.

Classify the user's message into exactly one intent:

  log_food     The message describes food or a meal that was eaten. Also extract
               the items: each with an English name, a state from raw, boiled,
               grilled, fried, baked, roasted, steamed, dried, liquid, unknown,
               and grams if the message states or clearly implies a amount.
               If grams are not stated, estimate a typical restaurant serving
               and set grams_estimated=true.
  log_water    The message is about drinking water (or another zero-calorie
               fluid). Extract ml.
  log_weight   The message reports a body weight in kg. Extract kg.
  status       The user asks what they have eaten, their budget, or their
               targets today.
  other        Anything else: greetings, questions about the bot, chat.

Never extract or report calories or macros. Those are computed downstream.
Quantities may be Turkish or English (e.g. "iki yumurta", "200 gram pilav",
"a glass of water"). A glass of water is 250ml unless stated otherwise."""

INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["log_food", "log_water", "log_weight", "status", "other"],
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "state": {"type": "string"},
                    "grams": {"type": "number"},
                    "grams_estimated": {"type": "boolean"},
                },
                "required": ["name", "state", "grams", "grams_estimated"],
            },
        },
        "ml": {"type": "number"},
        "kg": {"type": "number"},
    },
    "required": ["intent"],
}


@dataclass(slots=True)
class Classification:
    intent: str
    items: list[dict[str, Any]] = field(default_factory=list)
    ml: float | None = None
    kg: float | None = None


def parse_classification(content: str | None) -> Classification:
    raw = parse_json_object(content)
    intent = str(raw.get("intent", "other"))
    if intent not in ("log_food", "log_water", "log_weight", "status", "other"):
        intent = "other"

    items: list[dict[str, Any]] = []
    for it in raw.get("items") or []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        state = str(it.get("state", "unknown"))
        if state not in FoodState._value2member_map_:
            state = "unknown"
        try:
            grams = float(it.get("grams") or 0)
        except (TypeError, ValueError):
            grams = 0.0
        items.append(
            {
                "name": str(it["name"]).strip().lower(),
                "state": state,
                "grams": max(grams, 0.0),
                "grams_estimated": bool(it.get("grams_estimated", True)),
            }
        )

    def _opt_float(key: str) -> float | None:
        try:
            v = raw.get(key)
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return Classification(intent=intent, items=items, ml=_opt_float("ml"), kg=_opt_float("kg"))


# ---------------------------------------------------------------------------
# The reply layer
# ---------------------------------------------------------------------------

LOOKING_UP = "I'm still looking up some of these foods. I'll have the totals shortly."

HELP = """I keep track of what you eat, so you don't have to think about it.

Send a photo of your meal and I'll identify the items, estimate the amounts and log it.
You can also just type it: "200g rice and chicken" or "a coffee".

The buttons under the message field cover the daily routine:
💧 250 / 500 ml: log water in one tap
📊 Today: today's totals
✏️ Edit today: fix or remove something you logged
⚖️ Weigh in: log your morning weight

Commands:
/summary: where you are today
/week: the last seven days
/edit: fix or remove today's entries
/cuisines: what you usually eat
/dinnerware: plate sizes for better portion estimates
/recipe: save a dish you cook often
"""


@dataclass(slots=True)
class Reply:
    """A reply, plus the meal it created if it created one.

    The entry id is what lets a typed meal carry the same per-item correction
    keyboard a photographed one gets. Without it "200g rice and chicken" was
    uncorrectable, while the reply cheerfully suggested a /fix command that was
    never registered.
    """

    text: str
    entry_id: uuid.UUID | None = None
    # Whether anything in this meal is waiting on the enrichment job. A flag
    # rather than the caller sniffing the reply text for a phrase, which breaks
    # silently the day someone rewords the message.
    needs_enrichment: bool = False


async def handle_text(
    *,
    session: AsyncSession,
    models: ModelClient,
    user: User,
    clock: Clock,
    text: str,
) -> Reply:
    """One text message in, one reply out. Uses at most one model call."""

    # Bare numbers never reach the model. In weight context they are weigh-ins;
    # the common morning ritual of standing on the scale and typing a number.
    stripped = text.strip().replace(",", ".")
    try:
        value = float(stripped)
    except ValueError:
        value = None
    if value is not None and 30.0 <= value <= 250.0:
        return Reply(await _log_weight(session, user, clock, value))

    content, _served, _ms = await models.acall(
        "routing",
        messages=[
            {"role": "system", "content": INTENT_SYSTEM},
            {"role": "user", "content": _cuisine_hint(user) + text},
        ],
        schema=INTENT_SCHEMA,
    )
    c = parse_classification(content)

    if c.intent == "log_food" and c.items:
        return await _log_food(session, models, user, clock, c.items)
    if c.intent == "log_water":
        # A zero is the classifier failing to extract, not a logged nothing.
        if not c.ml or c.ml <= 0:
            return Reply("How much? Send it like '500 ml' or tap a water button.")
        await tools.log_simple(
            session,
            user.id,
            kind=EntryKind.water,
            value=c.ml,
            unit="ml",
            occurred_at=clock.now(),
            source=EntrySource.text,
        )
        return Reply(f"Water logged: {c.ml:.0f} ml 💧")
    if c.intent == "log_weight":
        if not c.kg or c.kg <= 0:
            return Reply("What did the scale say? Just send the number.")
        return Reply(await _log_weight(session, user, clock, c.kg))
    if c.intent == "status":
        return Reply(await summary_line(session, user, clock))
    return Reply(
        "I didn't catch that as something to log. Send a photo of the meal, "
        "or type it like '200g rice and grilled chicken'."
    )


def _cuisine_hint(user: User) -> str:
    """Prefix the classifier's input with the user's cuisines.

    Same reasoning as the perception prompt: "iki lahmacun" is two unknown
    words to a model reading cold and two portions of a named dish to one told
    this person eats Turkish food. Cheap — a dozen tokens on a call that already
    costs a fraction of a cent.
    """
    described = cuisines_mod.describe(user.cuisines or [])
    if not described:
        return ""
    return f"[This user mostly eats {described}. Their food names may be local.]\n"


async def _log_weight(session: AsyncSession, user: User, clock: Clock, kg: float) -> str:
    if not safety.is_plausible_weight(kg):
        return f"{kg} doesn't look like a body weight. If you meant it, send 'weighed {kg} kg'."
    await tools.log_simple(
        session,
        user.id,
        kind=EntryKind.weight,
        value=kg,
        unit="kg",
        occurred_at=clock.now(),
        source=EntrySource.text,
    )
    previous = await tools.previous_weight(session, user.id)
    if previous is not None:
        delta = kg - previous
        arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "→")
        return f"Logged {kg:.1f} kg {arrow} ({delta:+.1f} vs last)"
    return f"Logged {kg:.1f} kg"


async def _log_food(
    session: AsyncSession,
    models: ModelClient,
    user: User,
    clock: Clock,
    items: list[dict[str, Any]],
) -> Reply:
    resolver = Resolver(session, models)
    to_log: list[tools.ItemToLog] = []
    for it in items:
        resolution = await resolver.resolve(it["name"], FoodState(it["state"]), user.id)
        to_log.append(
            tools.ItemToLog(
                detected_name=it["name"],
                detected_state=FoodState(it["state"]),
                grams=it["grams"],
                # A typed log is never `vlm`: no vision model saw it. "200g
                # rice" is a weight the user stated; "some rice" is one the
                # classifier guessed. The calibration engine is entitled to
                # tell those apart, and stamping both as vlm lies to it.
                grams_source=(GramsSource.vlm if it["grams_estimated"] else GramsSource.user),
                grams_confidence=0.4 if it["grams_estimated"] else 1.0,
                resolution=resolution,
            )
        )
    meal = await tools.log_food_items(
        session,
        user.id,
        to_log,
        occurred_at=clock.now(),
        source=EntrySource.text,
        note=None,
    )
    await tools.remember(session, user.id, meal)

    reply = tools.format_meal(meal)
    if all(i["grams_estimated"] for i in items):
        reply += "\n(amounts estimated, tap ✏️ to correct one)"
    if meal.has_unmatched:
        reply += "\n\n" + LOOKING_UP
    return Reply(reply, meal.entry.id, needs_enrichment=meal.has_unmatched)


async def summary_line(session: AsyncSession, user: User, clock: Clock) -> str:
    totals = await tools.day_totals(session, user, clock)
    target = None
    weight = await tools.latest_weight(session, user.id)
    if weight is not None:
        try:
            target = tools.current_target(user, weight, clock)
        except RuntimeError:
            target = None
    steps = await tools.daily_steps(session, user, clock)
    return tools.format_day(user, totals, target, steps=steps)


async def week_summary(session: AsyncSession, user: User, clock: Clock) -> str:
    """The last seven days, trend language, no single-day judgement."""

    days: list[str] = []
    for back in range(7):
        day = local_date(clock.now(), user.tz) - dt.timedelta(days=back)
        totals = await tools.day_totals(session, user, clock, day)
        if totals.entry_count or totals.kcal:
            days.append(f"  {day:%a %d}: {totals.kcal:.0f} kcal, P {totals.protein_g:.0f}g")
    if not days:
        return "Nothing logged in the last seven days."
    return "Last seven days:\n" + "\n".join(reversed(days))


# ---------------------------------------------------------------------------
# The photo path, shared by the handler and used by tests through fixtures
# ---------------------------------------------------------------------------


async def log_photo(
    *,
    session: AsyncSession,
    models: ModelClient,
    user: User,
    clock: Clock,
    outcome: PerceptionOutcome,
    media_id: uuid.UUID | None = None,
    caption: str | None = None,
) -> Reply:
    """Stages 2-4 for a perception result."""
    resolver = Resolver(session, models, caption=caption)
    to_log = [
        tools.ItemToLog(
            detected_name=it.name,
            detected_state=FoodState(it.state),
            grams=it.grams,
            grams_source=GramsSource.vlm,
            grams_confidence=it.grams_confidence,
        )
        for it in outcome.result.items
    ]
    for item in to_log:
        item.resolution = await resolver.resolve(item.detected_name, item.detected_state, user.id)

    meal = await tools.log_food_items(
        session,
        user.id,
        to_log,
        occurred_at=_occurred_at(outcome, clock),
        source=EntrySource.photo,
    )
    await tools.remember(session, user.id, meal)

    # The raw response, the model that served it and the prompt fingerprint,
    # kept so old photos can be re-scored when a better model arrives and so
    # "did the prompt change or did the model?" stays answerable after the
    # fact. Dropping these on the floor made the fingerprint machinery in
    # perception/prompt.py decorative.
    session.add(
        PerceptionRun(
            media_id=media_id,
            entry_id=meal.entry.id,
            model=outcome.model,
            prompt_fingerprint=outcome.prompt_fingerprint,
            raw_response=outcome.raw,
            parsed_ok=True,
            latency_ms=outcome.latency_ms,
        )
    )
    if media_id is not None:
        await session.execute(
            update(Media).where(Media.id == media_id).values(entry_id=meal.entry.id)
        )

    reply = tools.format_meal(meal)
    if meal.has_unmatched:
        reply += "\n\n" + LOOKING_UP
    return Reply(reply, meal.entry.id, needs_enrichment=meal.has_unmatched)


def _occurred_at(outcome: PerceptionOutcome, clock: Clock) -> dt.datetime:
    """When the meal was eaten.

    Now, not the EXIF timestamp: a photo taken at lunch and sent at four in the
    afternoon is still a lunch, but a photo re-sent from the camera roll a week
    later is not a meal eaten a week ago, and there is no way to tell the two
    apart from the file. `media.taken_at` keeps the camera's answer for anyone
    who later wants to reconcile them.
    """
    del outcome
    return clock.now()
