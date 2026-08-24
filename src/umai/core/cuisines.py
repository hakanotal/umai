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


# ---------------------------------------------------------------------------
# Prompt anchors
# ---------------------------------------------------------------------------
#
# A vision model estimating grams needs something in the frame whose size it
# already knows, and the useful ones are the vessels and portions a person
# actually owns. Those are cultural: a Turkish tea glass is 110ml and a
# household that has never seen one gains nothing from being told so, while
# the household that has one gains a ruler.
#
# These used to be hardcoded Turkish in the shared system prompt, so every user
# was handed one country's crockery. They are rendered per user from the
# cuisines they picked, and they live in the *user* message rather than the
# system prompt: the system prompt is identical for everybody and stays that
# way, which keeps it stable, cacheable, and comparable across users.

# Vessel and portion sizes, per cuisine.
SCALE_REFERENCES: dict[str, tuple[str, ...]] = {
    "turkish": (
        "a tea glass (ince belli) is about 110ml",
        "a tea saucer is about 12cm across",
        "a standard simit is 90-110g",
    ),
    "levantine": (
        "a pita is 60-90g",
        "a mezze plate is 12-15cm across",
    ),
    "mediterranean": (
        "a Greek coffee cup is 60-90ml",
        "a slice of village bread is 40-60g",
    ),
    "italian": (
        "an espresso cup is 30-60ml",
        "a dry pasta portion is 80-100g, roughly doubling when cooked",
    ),
    "balkan": ("a rakija glass is 30-50ml", "a burek slice is 150-250g"),
    "caucasian": ("a khachapuri is 300-500g", "an armudu glass is about 100ml"),
    "persian": ("a tea estekan is 100-150ml", "a lavash sheet is 40-70g"),
    "indian": (
        "a chapati is 30-45g",
        "a katori (small bowl) holds 150-200ml",
        "a steel thali is 28-30cm across",
    ),
    "chinese": ("a rice bowl holds 250-300ml", "a tea cup is 50-100ml"),
    "japanese": (
        "a rice bowl (chawan) holds about 150g cooked rice",
        "a miso soup bowl is 200ml",
    ),
    "korean": ("a rice bowl holds about 200g", "a banchan dish is 8-10cm across"),
    "thai": ("a rice portion is 150-200g", "a soup bowl is 300-400ml"),
    "mexican": ("a corn tortilla is 25-30g", "a flour tortilla is 40-60g"),
    "american": (
        "a dinner plate is 26-28cm across",
        "a soda can is 330ml, a US cup is 240ml",
    ),
    "french": ("a baguette is 250g whole", "a wine glass pour is 125-175ml"),
    "german": ("a bread slice is 40-60g", "a beer glass is 300 or 500ml"),
    "british": ("a mug is 300-350ml", "a bread slice is 35-45g"),
    "northafrican": ("a tagine serves 2-4", "a glass of mint tea is about 100ml"),
    "vegan": (),
}

# What a model told nothing about the eater gets. Deliberately the things that
# are the same size everywhere.
NEUTRAL_SCALE_REFERENCES: tuple[str, ...] = (
    "a standard water glass is 250-300ml",
    "a drinks can is 330ml",
    "a bank card is 8.6cm long, and a dinner plate is usually 26-28cm across",
)

# One dish per cuisine that is sold and eaten as a single thing, so the model
# has a concrete instance of the "do not deconstruct a composite" rule. Naming
# a dish the user has never eaten teaches the rule just as well but wastes the
# example, which is why this is per cuisine too.
COMPOSITE_EXAMPLES: dict[str, tuple[str, str]] = {
    "turkish": ("Lahmacun", "flatbread plus minced meat plus parsley"),
    "levantine": ("A shawarma wrap", "bread plus meat plus sauce"),
    "mediterranean": ("Moussaka", "aubergine plus mince plus béchamel"),
    "italian": ("A margherita pizza", "dough plus tomato plus mozzarella"),
    "balkan": ("Burek", "pastry plus cheese"),
    "caucasian": ("Khachapuri", "bread plus cheese plus egg"),
    "persian": ("Ghormeh sabzi", "herbs plus lamb plus beans"),
    "indian": ("A masala dosa", "crepe plus potato filling"),
    "chinese": ("A pork bao", "dough plus filling"),
    "japanese": ("A salmon nigiri", "rice plus fish"),
    "korean": ("Bibimbap", "rice plus each vegetable separately"),
    "thai": ("Pad thai", "noodles plus egg plus peanuts"),
    "mexican": ("A taco al pastor", "tortilla plus pork plus pineapple"),
    "american": ("A cheeseburger", "bun plus patty plus cheese"),
    "french": ("A croque monsieur", "bread plus ham plus cheese"),
    "german": ("A doner", "bread plus meat plus salad"),
    "british": ("A cornish pasty", "pastry plus beef plus swede"),
    "northafrican": ("A lamb tagine", "meat plus each vegetable separately"),
    "vegan": ("A falafel wrap", "bread plus falafel plus tahini"),
}

NEUTRAL_COMPOSITE_EXAMPLE: tuple[str, str] = (
    "A sandwich",
    "bread plus filling plus spread",
)

# How people write quantities, for the free-text intent classifier. English is
# always understood; this adds the other language the user is likely to type in.
QUANTITY_EXAMPLES: dict[str, tuple[str, ...]] = {
    "turkish": ("iki yumurta", "200 gram pilav", "bir bardak ayran"),
    "levantine": ("kilo laban", "raghif khubz"),
    "italian": ("due uova", "un piatto di pasta"),
    "german": ("zwei Eier", "ein Glas Milch"),
    "french": ("deux oeufs", "un verre de lait"),
    "spanish": ("dos huevos", "un vaso de leche"),
    "persian": ("do tokhm-e morgh",),
    "indian": ("do roti", "ek katori dal"),
}


def scale_references(slugs: Iterable[str]) -> list[str]:
    """Vessel and portion anchors for these cuisines, or the neutral ones.

    Capped, because the list is a hint and not a reference manual: a model
    handed twenty measurements has been handed a document to skim rather than a
    ruler to use.
    """
    picked = normalise(slugs)
    out: list[str] = []
    for slug in picked:
        out.extend(SCALE_REFERENCES.get(slug, ()))
    return out[:6] if out else list(NEUTRAL_SCALE_REFERENCES)


def composite_example(slugs: Iterable[str]) -> tuple[str, str]:
    """A dish this person plausibly eats that must not be deconstructed."""
    for slug in normalise(slugs):
        if slug in COMPOSITE_EXAMPLES:
            return COMPOSITE_EXAMPLES[slug]
    return NEUTRAL_COMPOSITE_EXAMPLE


def quantity_examples(slugs: Iterable[str]) -> list[str]:
    """Phrasings the free-text classifier should expect, beyond English."""
    out: list[str] = []
    for slug in normalise(slugs):
        out.extend(QUANTITY_EXAMPLES.get(slug, ()))
    return out[:4]


# A named dish beside the description a model produces when it does not
# recognise it. The contrast is the single most useful thing in the whole
# prompt — it is the difference between a lookup key and 78 characters of prose
# that match nothing — and it lands hardest when the dish is one the reader's
# photos actually contain.
DISH_NAME_EXAMPLES: dict[str, tuple[str, str]] = {
    "turkish": ("lahmacun", "flatbread with reddish meat and pepper paste topping"),
    "levantine": ("fattoush", "chopped salad with fried bread pieces"),
    "mediterranean": ("spanakopita", "layered pastry with green filling"),
    "italian": ("carbonara", "pasta in a pale egg and bacon sauce"),
    "balkan": ("cevapi", "small grilled minced-meat fingers"),
    "caucasian": ("khinkali", "large pleated boiled dumplings"),
    "persian": ("tahdig", "crisp golden layer of rice from the pan bottom"),
    "indian": ("dosa", "large thin crisp fermented crepe"),
    "chinese": ("mapo tofu", "cubed tofu in a red oily sauce with mince"),
    "japanese": ("okonomiyaki", "thick savoury pancake with brown sauce and flakes"),
    "korean": ("kimchi jjigae", "red soup with cabbage and pork"),
    "thai": ("som tam", "shredded green papaya salad"),
    "mexican": ("chilaquiles", "fried tortilla pieces in red sauce"),
    "american": ("sloppy joe", "bun with loose sauced mince"),
    "french": ("ratatouille", "stewed mixed summer vegetables"),
    "german": ("spaetzle", "irregular soft egg noodles"),
    "british": ("bubble and squeak", "fried leftover potato and cabbage patty"),
    "northafrican": ("shakshuka", "eggs poached in a red pepper and tomato sauce"),
    "vegan": ("falafel", "fried brown balls of ground pulses"),
}

NEUTRAL_DISH_NAME_EXAMPLE: tuple[str, str] = (
    "lasagne",
    "layered pasta with mince and white sauce",
)


def dish_name_example(slugs: Iterable[str]) -> tuple[str, str]:
    """(the name to write, the description to avoid writing)."""
    for slug in normalise(slugs):
        if slug in DISH_NAME_EXAMPLES:
            return DISH_NAME_EXAMPLES[slug]
    return NEUTRAL_DISH_NAME_EXAMPLE


def local_names(slugs: Iterable[str]) -> list[str]:
    """Dish names to keep untranslated, drawn from the user's own traditions.

    A model that translates "mercimek corbasi" to "lentil soup" has thrown away
    the lookup key, and which names those are depends entirely on who is
    eating.
    """
    return [DISH_NAME_EXAMPLES[s][0] for s in normalise(slugs) if s in DISH_NAME_EXAMPLES]
