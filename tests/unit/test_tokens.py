"""The two credentials, and the ways they are meant to fail."""

from __future__ import annotations

from umai.core import tokens


def test_tokens_are_unique_and_fit_the_column():
    made = {tokens.new_token() for _ in range(200)}
    assert len(made) == 200
    assert all(len(t) <= 64 for t in made)
    assert all(t.isascii() and " " not in t for t in made)


def test_phrase_matching_ignores_case_and_stray_whitespace():
    """A phrase typed into a chat window arrives with the sender's
    capitalisation and whatever spacing their keyboard produced. Rejecting
    those teaches the sender only that the bot is broken."""
    assert tokens.phrase_matches("  Open   Sesame ", "open sesame")
    assert tokens.phrase_matches("OPEN SESAME", "open sesame")


def test_a_wrong_phrase_does_not_match():
    assert not tokens.phrase_matches("open sesam", "open sesame")
    assert not tokens.phrase_matches("", "open sesame")


def test_an_unset_expected_phrase_never_matches():
    """The load-bearing one. If UMAI_INVITE_CODE were unset and an empty
    expected value compared equal to an empty message, the bot would admit
    anyone who sent it a blank line."""
    assert not tokens.phrase_matches("", "")
    assert not tokens.phrase_matches("anything", "")
