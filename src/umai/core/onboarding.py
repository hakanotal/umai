"""The onboarding wizard's brains: what to ask, in what order, and how to read
the answer. No Telegram, no database, no clock.

Everything the calibration engine and the safety rails need about a person —
their zone, sex, height, birth date, starting weight and goal — used to arrive
from environment variables and be copied onto every row that `get_or_create_user`
created. That works for exactly one person. This module is what replaces it:
the same fields, asked once, in chat, per user.

**The wizard holds no state of its own.** `next_step` derives the current
question from the first field on the row that is still unset, so the answer to
"where were we" is the data itself and there is nothing that can disagree with
it. That matters more than it sounds: aiogram's default FSM storage is
in-memory, so a restart mid-wizard would drop someone into `handlers/text.py`'s
catch-all, where their reply of "180" is a plausible meal. Deriving the step
means a restart resumes exactly where it left off, for free.

Order is not arbitrary. The timezone comes first because every later prompt and
every subsequent local day depends on it, and because it is the only answer
that is invisible when wrong — the others are obviously wrong on sight, a bad
zone shows up weeks later as a day that landed on the wrong date.

Weight is asked but not written here. It is not a column on `users`; it is the
first point of the weight series, and it is written once at the end of the
wizard so an abandoned run leaves no orphan reading behind (see
`handlers/onboarding.py`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from umai.analytics import safety
from umai.core import timezones

# --- what an answer parses into --------------------------------------------


@dataclass(frozen=True, slots=True)
class Ok:
    """A parsed answer, ready to be written to the field."""

    value: Any


@dataclass(frozen=True, slots=True)
class Retry:
    """A rejected answer, with the sentence to send back.

    Carries its own message because the reason for rejection is the useful
    part: "that reads as 1.8 kg" tells someone what to do, "invalid input"
    does not.
    """

    message: str


ParseResult = Ok | Retry


# --- bounds -----------------------------------------------------------------

MIN_HEIGHT_CM, MAX_HEIGHT_CM = 100.0, 250.0
# 13 is Telegram's own minimum age, so anything below it is a typo rather than
# a user. 100 catches a mistyped year without insulting anyone plausible.
MIN_AGE_YEARS, MAX_AGE_YEARS = 13, 100

# Presented as buttons, not free text. People type "1.5" for kg per week, the
# safety rails silently cap it at 1% of body weight, and the user is left
# believing a target they were never given. A keyboard makes the cap honest
# before it is applied rather than after.
GOAL_CHOICES: tuple[tuple[str, float, str], ...] = (
    ("Lose steadily (0.75 kg/wk)", -0.75, "lose"),
    ("Lose (0.5 kg/wk)", -0.5, "lose"),
    ("Lose slowly (0.25 kg/wk)", -0.25, "lose"),
    ("Maintain", 0.0, "maintain"),
    ("Gain slowly (0.25 kg/wk)", 0.25, "gain"),
)

GoalType = Literal["lose", "maintain", "gain"]


def goal_type_for(rate: float) -> GoalType:
    if rate < 0:
        return "lose"
    if rate > 0:
        return "gain"
    return "maintain"


# --- parsers ----------------------------------------------------------------


def _number(text: str) -> float | None:
    """The number in a free-text answer, or None.

    Strips the unit people habitually append ("180cm", "88 kg") and accepts a
    comma decimal separator, which is what a Turkish or German keyboard
    produces and what every naive `float()` call rejects.

    A leading minus survives, because the goal rates are negative. It is not
    stripped for the other steps either: a negative height or weight then fails
    its own bounds check, which is a better answer than silently reading -180
    as 180.
    """
    stripped = text.strip()
    sign = -1.0 if stripped.startswith("-") else 1.0
    cleaned = "".join(c for c in stripped if c.isdigit() or c in ",.").replace(",", ".")
    if cleaned.count(".") > 1 or not cleaned.strip("."):
        return None
    try:
        return sign * float(cleaned)
    except ValueError:
        return None


def parse_tz(text: str) -> ParseResult:
    """A city name, or an IANA zone typed verbatim.

    Several candidates are returned as-is for the caller to offer as buttons;
    it is not this function's job to guess between two real places.
    """
    found = timezones.resolve_city(text)
    if not found:
        return Retry(
            "I don't know that one. Try a nearby big city — Istanbul, London, "
            "Berlin, New York — or the full zone name like Europe/Istanbul."
        )
    if len(found) > 1:
        return Ok(found)
    return Ok(found[0])


def parse_sex(text: str) -> ParseResult:
    value = text.strip().casefold()
    if value in {"male", "m", "man", "erkek"}:
        return Ok("male")
    # Both Turkish spellings: casefold("KADIN") is "kadin", but someone
    # typing it in lower case produces the dotless-i spelling.
    if value in {"female", "f", "woman", "kadın", "kadin"}:  # noqa: RUF001
        return Ok("female")
    return Retry("Tap one of the buttons, or send 'male' or 'female'.")


def parse_height(text: str) -> ParseResult:
    n = _number(text)
    if n is None:
        return Retry("Send your height as a number, like 180.")
    # Metres, near-universally typed as 1.80. Converted rather than rejected:
    # it is unambiguous, because nobody is 1.8 cm tall.
    if n < 3:
        n *= 100
    if not MIN_HEIGHT_CM <= n <= MAX_HEIGHT_CM:
        return Retry(f"That reads as {n:.0f} cm. Send your height in centimetres, like 180.")
    return Ok(n)


def parse_birth_date(text: str) -> ParseResult:
    """ISO first, then the day-first form a Turkish or European keyboard types.

    Deliberately no month-first format. `03/04/1990` is March in one convention
    and April in the other, and a birth date silently off by a month shifts
    every age and therefore every BMR the system will ever compute for this
    person. Refusing an ambiguous input is the only honest option.
    """
    raw = text.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            parsed = dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        return Ok(parsed)
    return Retry("Send it as YYYY-MM-DD, for example 1990-05-01.")


def parse_weight(text: str) -> ParseResult:
    n = _number(text)
    if n is None:
        return Retry("Send your weight in kilograms as a number, like 88.")
    if not safety.is_plausible_weight(n):
        return Retry(f"{n:.1f} kg doesn't look right. Send your weight in kilograms.")
    return Ok(n)


def parse_goal(text: str) -> ParseResult:
    n = _number(text)
    if n is None:
        return Retry("Tap one of the buttons.")
    for _, rate, _kind in GOAL_CHOICES:
        if abs(rate - n) < 1e-9:
            return Ok(rate)
    return Retry("Tap one of the buttons.")


def parse_cuisines(_: str) -> ParseResult:
    """Cuisines arrive by button only; a typed reply is a nudge, not an error."""
    return Retry("Tap the cuisines you eat, then tap Done.")


# --- the sequence -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Step:
    """One question.

    `field` is the column on `users` whose being unset means this step is
    still owed — that is the whole state machine. `kind` tells the handler
    which keyboard to draw; keeping it a tag rather than a keyboard object is
    what keeps this module free of aiogram.
    """

    field: str
    prompt: str
    parse: Callable[[str], ParseResult]
    kind: Literal["text", "sex", "goal", "cuisines", "tz"]


STEPS: tuple[Step, ...] = (
    Step(
        field="tz",
        prompt=(
            "Welcome. Six quick questions and then you can start logging.\n\n"
            "Which city do you live in? I need it to know when your day starts "
            "and ends — everything I total up depends on it."
        ),
        parse=parse_tz,
        kind="tz",
    ),
    Step(
        field="sex",
        prompt=(
            "Male or female? This goes into the formula that estimates how much "
            "energy you burn at rest, and into the safety floor I refuse to set "
            "a target below."
        ),
        parse=parse_sex,
        kind="sex",
    ),
    Step(
        field="height_cm",
        prompt="How tall are you, in centimetres?",
        parse=parse_height,
        kind="text",
    ),
    Step(
        field="birth_date",
        prompt="What's your date of birth? YYYY-MM-DD, for example 1990-05-01.",
        parse=parse_birth_date,
        kind="text",
    ),
    Step(
        field="onboarding_weight_kg",
        prompt=(
            "What do you weigh right now, in kilograms? Roughly is fine — it "
            "starts the trend line, and the trend is what matters, not the "
            "single reading."
        ),
        parse=parse_weight,
        kind="text",
    ),
    Step(
        field="goal_rate_kg_per_week",
        prompt="What are you aiming for?",
        parse=parse_goal,
        kind="goal",
    ),
    Step(
        field="cuisines",
        prompt=(
            "Last one. What do you usually eat? Tap all that apply, then Done.\n\n"
            "I hand this to the model that reads your photos, so it recognises "
            "your dishes by name instead of describing them — which is the "
            "difference between logging a meal and logging nothing."
        ),
        parse=parse_cuisines,
        kind="cuisines",
    ),
)

# The weight step has no column of its own: weight lives in the log, not on the
# user row. The handler stashes it here on the row-shaped object it passes in,
# and writes the log entry once, at the end.
WEIGHT_FIELD = "onboarding_weight_kg"


def _is_set(value: Any) -> bool:
    """Unset means None, or — for cuisines — an empty list.

    An empty list has to count as unset or the cuisine step, whose column has a
    server default of `{}`, would be skipped for everyone.
    """
    if value is None:
        return False
    if isinstance(value, list | tuple):
        return len(value) > 0
    return True


def next_step(state: Any) -> Step | None:
    """The first step whose field is still unset, or None when the wizard is
    done. `state` is anything with the fields as attributes — a `User` row in
    production, a simple stand-in in the tests."""
    for step in STEPS:
        if not _is_set(getattr(state, step.field, None)):
            return step
    return None


def progress(state: Any) -> tuple[int, int]:
    """(question number, total), for the "3 of 7" in the prompt."""
    step = next_step(state)
    if step is None:
        return len(STEPS), len(STEPS)
    return STEPS.index(step) + 1, len(STEPS)
