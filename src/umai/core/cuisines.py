"""The cuisines a user actually eats, and what they are worth.

A vision model asked to read a plate cold produces descriptions: "flatbread
with reddish meat/pepper paste topping". Told that this person eats Turkish
food, the same model produces an identification: "lahmacun". The difference is
not cosmetic. A description is 78 characters of prose that trigram similarity
cannot match against any row in a food composition table; a name is a lookup
key, and it is also the thing the enrichment job can research when the table
does not hold it yet.

So the list is used in three places, and it is the same list in all three:

  * stage 1, as a hint in the perception prompt (`perception/prompt.py`)
  * the text classifier, so "iki lahmacun" is read as a dish rather than two
    unknown words (`core/agent.py`)
  * the enrichment job, which needs to know that "pide" is a Turkish flatbread
    and not an Italian one before it recalls a composition (`core/enrichment.py`)

Deliberately a short curated list rather than free text. The value is in the
model recognising a culinary tradition it has deep knowledge of, and a typo or
an invented cuisine buys nothing while quietly degrading every photo. Users who
eat something not listed here lose nothing: an empty list is the old behaviour.
"""

from __future__ import annotations

from collections.abc import Iterable

# slug -> (button label, the phrasing handed to a model)
CUISINES: dict[str, tuple[str, str]] = {
    "turkish": ("🇹🇷 Turkish", "Turkish and Ottoman home and restaurant cooking"),
    "levantine": ("🥙 Levantine", "Levantine and Middle Eastern (Syrian, Lebanese, Palestinian)"),
    "mediterranean": ("🫒 Mediterranean", "Mediterranean (Greek, coastal Italian, Spanish)"),
    "italian": ("🍝 Italian", "Italian"),
    "balkan": ("🥟 Balkan", "Balkan (Bosnian, Serbian, Bulgarian, Albanian)"),
    "caucasian": ("🍢 Caucasian", "Caucasian and Georgian"),
    "persian": ("🍚 Persian", "Persian and Iranian"),
    "indian": ("🍛 Indian", "Indian and South Asian"),
    "chinese": ("🥢 Chinese", "Chinese"),
    "japanese": ("🍣 Japanese", "Japanese"),
    "korean": ("🍲 Korean", "Korean"),
    "thai": ("🌶 Thai", "Thai and South-East Asian"),
    "mexican": ("🌮 Mexican", "Mexican and Latin American"),
    "american": ("🍔 American", "American diner and barbecue"),
    "french": ("🥐 French", "French"),
    "german": ("🥨 German", "German, Austrian and Central European"),
    "british": ("🫖 British", "British and Irish"),
    "northafrican": ("🍯 N. African", "North African (Moroccan, Tunisian, Egyptian)"),
    "vegan": ("🌱 Plant-based", "plant-based and vegan cooking"),
}

# Beyond this the hint stops being a hint. A model told the user eats fifteen
# cuisines has been told nothing, and the prompt is longer for it.
MAX_CUISINES = 6


def normalise(slugs: Iterable[str]) -> list[str]:
    """Keep the known slugs, in the canonical order, without duplicates.

    Order comes from CUISINES rather than from the user's clicks so that the
    prompt is stable across sessions: an unstable prompt is an unstable
    fingerprint, and `perception_runs` exists to tell prompt drift from model
    drift.
    """
    wanted = {s.strip().lower() for s in slugs if s and s.strip()}
    return [slug for slug in CUISINES if slug in wanted][:MAX_CUISINES]


def label(slug: str) -> str:
    return CUISINES[slug][0] if slug in CUISINES else slug


def describe(slugs: Iterable[str]) -> str:
    """The phrasing handed to a model. Empty when nothing is selected, so the
    caller can leave the hint out entirely rather than say "no cuisines"."""
    picked = normalise(slugs)
    if not picked:
        return ""
    return ", ".join(CUISINES[s][1] for s in picked)


def toggle(current: Iterable[str], slug: str) -> tuple[list[str], str | None]:
    """Turn one cuisine on or off, and say what happened.

    Returns the new list and a short note for the tap acknowledgement, or
    `(unchanged, None)` when the limit is in the way — the caller decides how
    loudly to say so, because a toast and an alert are different messages.

    Extracted so the two pickers cannot drift. `/cuisines` and the onboarding
    wizard draw the same grid behind opposite access filters, and duplicating
    the limit check would eventually mean one of them enforcing a different
    maximum than the other.
    """
    picked = list(current)
    if slug in picked:
        picked.remove(slug)
        return normalise(picked), f"{label(slug)} off"
    if len(picked) >= MAX_CUISINES:
        return picked, None
    picked.append(slug)
    # Normalised on write so the prompt is stable across sessions: an unstable
    # prompt is an unstable fingerprint, and perception_runs exists to tell
    # prompt drift from model drift.
    return normalise(picked), f"{label(slug)} on"
