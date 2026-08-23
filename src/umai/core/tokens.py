"""Secrets the bot hands out, and the phrase it checks people against.

Two unrelated credentials share this module because they share one property:
both are compared against something a stranger controls, and both must be
compared in constant time. Everything else about them differs.

The **invite phrase** is one shared secret, configured once in the environment
and typed by every person who wants in. It is never stored — the environment
holds it, `phrase_matches` compares against it, and nothing writes it to a
row. Comparison normalises first, because a phrase that travels through a chat
message arrives with the sender's capitalisation and whatever whitespace their
keyboard inserted, and rejecting "Open Sesame" when the phrase is "open sesame"
teaches the sender nothing except that the bot is broken.

The **health token** is per-user, generated once at the end of onboarding, and
stored in plaintext on the row so `/token` can show it again — a deliberate
choice for a self-hosted deployment where the alternative (a hash, shown once)
means a lost token is a rotation and a re-paste into a phone app. It is a
bearer credential for one person's health series, so it is generated from
`secrets`, never from `random`.
"""

from __future__ import annotations

import hmac
import secrets

# 32 bytes of entropy, url-safe, ~43 characters. Comfortably inside the
# String(64) column and inside an HTTP header without quoting.
TOKEN_BYTES = 32


def new_token() -> str:
    """A fresh per-user health-ingest token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def normalise_phrase(phrase: str) -> str:
    """Casefold and collapse whitespace.

    `casefold`, not `lower`: it is the one that handles the cases `lower` gets
    wrong (German ß, Turkish dotted/dotless I), and the operator of this
    particular bot types Turkish.
    """
    return " ".join(phrase.casefold().split())


def phrase_matches(given: str, expected: str) -> bool:
    """Constant-time comparison of two normalised phrases.

    An empty `expected` never matches. Without that guard an unset
    `UMAI_INVITE_CODE` would admit anyone who sent an empty message, which is
    the exact opposite of what the setting is for. `check_startup` refuses to
    boot in that state as well; this is the second lock on the same door.
    """
    if not expected:
        return False
    return hmac.compare_digest(normalise_phrase(given), normalise_phrase(expected))
