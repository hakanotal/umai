"""The stage 1 Pydantic schema: items, state, grams, confidences.

One definition. `model_json_schema()` feeds the API directly so the schema the
model is given and the type the code parses cannot drift apart.

Note what is absent: kcal, protein, carbs, fat. Requesting them is what breaks
the design, and the plan is explicit that if the model returns them anyway they
are discarded. `extra="ignore"` is how that discarding happens.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FoodStateLiteral = Literal[
    "raw", "boiled", "grilled", "fried", "baked", "roasted", "steamed", "dried", "liquid", "unknown"
]

# A single item weighing more than this is a hallucination or a units error, not
# a meal. Clamped rather than rejected: one absurd item should not throw away
# the other four the model got right.
MAX_PLAUSIBLE_ITEM_GRAMS = 5000.0

# The name is a lookup key, not a caption. Trigram similarity between a
# 78-character description and a table row is indistinguishable from noise:
# the first live session produced "flatbread with reddish meat/pepper paste
# topping (turkish pide-style flatbread)" and "fresh parsley / cilantro
# (garnish on flatbread)", neither of which could match anything, so both were
# logged at zero calories. Anything the model wants to add beyond the name goes
# in `description`, which the resolver never sees.
MAX_NAME_WORDS = 4
MAX_NAME_CHARS = 60


class DetectedItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(
        description=(
            "The dish or ingredient name alone, 1-4 words, no parenthetical "
            "asides and no positional qualifiers. This is a database lookup key."
        )
    )
    description: str | None = Field(
        default=None,
        description=(
            "Anything else worth saying about the item: garnishes, how it was "
            "plated, what it sat on. Never part of the name."
        ),
    )
    state: FoodStateLiteral = Field(description="How it was cooked")
    grams: float = Field(ge=0, description="Estimated weight; millilitres converted for liquids")
    grams_confidence: float = Field(ge=0, le=1)
    identity_confidence: float = Field(ge=0, le=1)
    reference_used: str | None = Field(
        default=None,
        description="The scale cue used, or a statement that there was none",
    )

    @field_validator("grams")
    @classmethod
    def _clamp_absurd(cls, v: float) -> float:
        return min(v, MAX_PLAUSIBLE_ITEM_GRAMS)

    @field_validator("name")
    @classmethod
    def _tidy(cls, v: str) -> str:
        """Lower-case, collapse whitespace, and enforce the key-not-caption rule.

        Belt and braces over the prompt, which is what should really be doing
        this work: a model that ignores the instruction and returns prose would
        otherwise poison resolution silently, and silently is the failure mode
        this whole module is arranged against.

        Deliberately conservative. Only the mechanical decorations come off —
        the parenthetical aside, the "X with Y" tail, the overlong tail — all
        of which put the useful name first in every observed violation. A
        slash is left alone: "smoked/charred beef" trimmed at the slash is
        "smoked", which is worse than the phrase it replaced.
        """
        name = " ".join(v.strip().lower().split())
        if "(" in name:
            name = name.split("(", 1)[0].strip()
        for separator in (",", " with ", " on ", " in ", " under ", " topped "):
            if separator in name:
                name = name.split(separator, 1)[0].strip()
        words = name.split()
        if len(words) > MAX_NAME_WORDS:
            name = " ".join(words[:MAX_NAME_WORDS])
        return name[:MAX_NAME_CHARS].strip(" -,")


class PerceptionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[DetectedItem] = Field(default_factory=list)
    clarifying_question: str | None = None
    overall_confidence: float = Field(default=0.0, ge=0, le=1)

    @property
    def total_grams(self) -> float:
        return sum(i.grams for i in self.items)

    @property
    def weakest_grams_confidence(self) -> float:
        return min((i.grams_confidence for i in self.items), default=0.0)

    def needs_confirmation(self, threshold: float = 0.75) -> bool:
        """Whether to ask before logging.

        The eval harness decides the threshold: at a ratio SD above 0.20 the
        plan's instruction is to always confirm grams and never log a photo
        silently, which is what a threshold of 1.0 expresses.
        """
        return self.weakest_grams_confidence < threshold or self.overall_confidence < threshold


def request_schema() -> dict[str, Any]:
    """The JSON schema handed to the model.

    Built from the Pydantic model rather than written by hand, so adding a field
    in one place cannot leave the prompt describing the old shape.
    """
    schema = PerceptionResult.model_json_schema()
    _strictify(schema)
    return schema


def _strictify(node: Any) -> None:
    """OpenAI-style strict schemas reject unspecified additional properties and
    require every property to be listed. Applied recursively, in place."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for value in node.values():
            _strictify(value)
    elif isinstance(node, list):
        for item in node:
            _strictify(item)
