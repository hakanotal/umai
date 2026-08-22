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
