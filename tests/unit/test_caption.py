"""Caption-as-context tests.

When a user sends a photo with a caption (e.g. "lahmacun"), the caption is
injected into the vision prompt via PromptContext.note and into the resolver's
tiebreak message. These tests verify both paths without hitting a model API.
"""

from __future__ import annotations

from umai.perception.prompt import PromptContext, build

# ---------------------------------------------------------------------------
# Prompt build()
# ---------------------------------------------------------------------------


def test_note_appears_in_prompt():
    ctx = PromptContext(note="lahmacun")
    prompt = build(ctx)
    assert "The user said: lahmacun" in prompt


def test_no_note_when_none():
    ctx = PromptContext()
    prompt = build(ctx)
    assert "The user said:" not in prompt


def test_no_note_when_empty_string():
    ctx = PromptContext(note="")
    prompt = build(ctx)
    assert "The user said:" not in prompt


def test_note_combines_with_cuisines():
    ctx = PromptContext(cuisines=["turkish"], note="menemen")
    prompt = build(ctx)
    assert "This user mostly eats" in prompt
    assert "The user said: menemen" in prompt


def test_note_is_last_before_final_instruction():
    ctx = PromptContext(note="pide")
    prompt = build(ctx)
    lines = prompt.strip().split("\n\n")
    assert lines[-1] == "Identify the items in this photo and estimate the weight of each."
    assert lines[-2] == "The user said: pide"


# ---------------------------------------------------------------------------
# Prompt anchors are the user's, not one country's
# ---------------------------------------------------------------------------


def test_the_system_prompt_names_no_single_cuisine():
    """The regression guard.

    A Turkish tea glass and a lahmacun were hardcoded into the prompt every
    user shares, so a household in Osaka was told the size of crockery it does
    not own and given a deconstruction rule about a dish it has never eaten.
    Anything cuisine-specific belongs in the per-user message instead.
    """
    from umai.perception.prompt import SYSTEM

    lowered = SYSTEM.lower()
    for word in ("turkish", "lahmacun", "ince belli", "simit", "pilav"):
        assert word not in lowered, f"{word!r} is in the shared system prompt"


def test_scale_references_follow_the_users_cuisines():
    from umai.core import cuisines as c

    turkish = " ".join(c.scale_references(["turkish"]))
    japanese = " ".join(c.scale_references(["japanese"]))
    assert "tea glass" in turkish
    assert "chawan" in japanese
    assert turkish != japanese


def test_a_user_with_no_cuisines_gets_universal_anchors():
    """The thing this is really for: a person who has not picked a cuisine, or
    whose cooking is not on the list, must not be handed somebody else's
    crockery. What is left is what is the same size everywhere."""
    from umai.core import cuisines as c

    refs = c.scale_references([])
    assert refs == list(c.NEUTRAL_SCALE_REFERENCES)
    assert any("bank card" in r for r in refs)


def test_the_composite_example_is_a_dish_the_user_plausibly_eats():
    from umai.core import cuisines as c

    assert c.composite_example(["turkish"])[0] == "Lahmacun"
    assert c.composite_example(["mexican"])[0].startswith("A taco")
    assert c.composite_example([]) == c.NEUTRAL_COMPOSITE_EXAMPLE


def test_the_built_prompt_carries_the_users_anchors():
    from umai.perception.prompt import PromptContext, build

    turkish = build(PromptContext(cuisines=["turkish"]))
    neutral = build(PromptContext())

    assert "ince belli" in turkish and "Lahmacun" in turkish
    assert "ince belli" not in neutral and "Lahmacun" not in neutral
    assert "bank card" in neutral


def test_the_fingerprint_separates_two_users_prompts():
    """`perception_runs.prompt_fingerprint` exists to tell prompt drift from
    model drift. Now that the prompt varies per user it has to vary with it, or
    two users' runs would be indistinguishable in the table."""
    from umai.perception.prompt import PromptContext, build, fingerprint

    a = fingerprint("sys", build(PromptContext(cuisines=["turkish"])))
    b = fingerprint("sys", build(PromptContext(cuisines=["japanese"])))
    assert a != b


def test_scale_references_are_capped():
    """A hint, not a reference manual: a model handed twenty measurements has
    been handed a document to skim rather than a ruler to use."""
    from umai.core import cuisines as c

    many = c.scale_references(["turkish", "indian", "japanese", "american", "italian", "french"])
    assert len(many) <= 6
