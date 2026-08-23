"""Turning "istanbul" into "Europe/Istanbul".

The onboarding wizard has to ask for a timezone, and a timezone is the single
hardest thing in this whole flow to ask a person for in a chat window. Nobody
knows their IANA zone name. Everybody knows their city.

So the wizard asks for a city and this module resolves it, by matching the
normalised input against the last path segment of every zone the tz database
knows — `Europe/Istanbul` matches "istanbul", `America/New_York` matches "new
york" once the underscore is folded to a space. That covers most of the world
for free, because the tz database is a list of cities.

What it does not cover is the way people actually name where they live: the
abbreviation ("nyc", "sf"), the city that is not itself a zone but shares one
("izmir", "manchester"), and the country used as a city ("singapore" happens to
work, "netherlands" does not). `ALIASES` handles those by hand. It is
deliberately short — every entry is a guess about someone else's vocabulary,
and the wizard's fallback (offer a shortlist, accept a raw IANA name) is a
better answer than a long list of guesses.

What it also does is refuse to offer the tz database's legacy names. Left
unfiltered, "istanbul" comes back as both `Asia/Istanbul` and
`Europe/Istanbul`, "central" as both `US/Central` and `Canada/Central`, and
asking a person to choose between two spellings of the same place is a worse
question than the one being answered. Only zones under a genuine geographic
region are indexed, and the handful of same-city pairs that survive that
(`Europe/Istanbul` and `Asia/Istanbul`, `America/Argentina/Buenos_Aires` and
`America/Buenos_Aires`) are behaviourally identical links, so `_preferred`
picks one deterministically. `test_timezones.py` asserts that every surviving
collision really is identical — if the tz database ever grows a genuine one,
that test fails rather than the wizard silently picking a side.
"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import available_timezones

# Things people type that are not the last segment of any zone name. Values are
# full IANA names and are asserted to exist by the unit tests, so a rename in
# the tz database surfaces as a test failure rather than as a wizard that
# cannot resolve a city it claims to know.
ALIASES: dict[str, str] = {
    "nyc": "America/New_York",
    "new york city": "America/New_York",
    "sf": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "bay area": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "washington dc": "America/New_York",
    "dc": "America/New_York",
    "boston": "America/New_York",
    "philadelphia": "America/New_York",
    "seattle": "America/Los_Angeles",
    "austin": "America/Chicago",
    "izmir": "Europe/Istanbul",
    "ankara": "Europe/Istanbul",
    "bursa": "Europe/Istanbul",
    "antalya": "Europe/Istanbul",
    "adana": "Europe/Istanbul",
    "turkey": "Europe/Istanbul",
    "turkiye": "Europe/Istanbul",
    "manchester": "Europe/London",
    "birmingham": "Europe/London",
    "edinburgh": "Europe/London",
    "glasgow": "Europe/London",
    "uk": "Europe/London",
    "england": "Europe/London",
    "scotland": "Europe/London",
    "munich": "Europe/Berlin",
    "munchen": "Europe/Berlin",
    "hamburg": "Europe/Berlin",
    "cologne": "Europe/Berlin",
    "frankfurt": "Europe/Berlin",
    "germany": "Europe/Berlin",
    "netherlands": "Europe/Amsterdam",
    "holland": "Europe/Amsterdam",
    "rotterdam": "Europe/Amsterdam",
    "france": "Europe/Paris",
    "spain": "Europe/Madrid",
    "barcelona": "Europe/Madrid",
    "italy": "Europe/Rome",
    "milan": "Europe/Rome",
    "milano": "Europe/Rome",
    "switzerland": "Europe/Zurich",
    "geneva": "Europe/Zurich",
    "sweden": "Europe/Stockholm",
    "norway": "Europe/Oslo",
    "denmark": "Europe/Copenhagen",
    "finland": "Europe/Helsinki",
    "poland": "Europe/Warsaw",
    "portugal": "Europe/Lisbon",
    "porto": "Europe/Lisbon",
    "greece": "Europe/Athens",
    "japan": "Asia/Tokyo",
    "osaka": "Asia/Tokyo",
    "kyoto": "Asia/Tokyo",
    "korea": "Asia/Seoul",
    "india": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "bombay": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "new delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata",
    "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "shenzhen": "Asia/Shanghai",
    "uae": "Asia/Dubai",
    "abu dhabi": "Asia/Dubai",
    "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "australia": "Australia/Sydney",
    "toronto": "America/Toronto",
    "montreal": "America/Toronto",
    "vancouver": "America/Vancouver",
    "canada": "America/Toronto",
    "brazil": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo",
    "mexico": "America/Mexico_City",
    "cdmx": "America/Mexico_City",
    "south africa": "Africa/Johannesburg",
    "cape town": "Africa/Johannesburg",
    "nigeria": "Africa/Lagos",
    "egypt": "Africa/Cairo",
    "israel": "Asia/Jerusalem",
    "tel aviv": "Asia/Jerusalem",
}


# The tz database's geographic top level. Everything else in it — `US/`,
# `Canada/`, `Brazil/`, `Etc/`, and the bare names like `Singapore` and `GMT` —
# is a backwards-compatibility link, and indexing those turns "central" into a
# choice between two spellings of one place.
REGIONS = (
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)

# Tiebreak for same-city pairs that survive the region filter. Both members of
# every such pair are links to one another, so either answer is correct and
# this only decides which spelling a person is shown. Europe before Asia
# settles `Europe/Istanbul` and `Europe/Nicosia`, which are the canonical ones.
_REGION_RANK = {"Europe": 0, "America": 1, "Asia": 2}


def normalise(text: str) -> str:
    """Casefold, fold underscores and hyphens to spaces, collapse whitespace.

    `casefold` rather than `lower` for the Turkish dotless I: "İZMİR" lowers to
    "i\u0307zmir" with a combining dot, which matches nothing.
    """
    folded = text.casefold().replace("_", " ").replace("-", " ")
    return " ".join(folded.split())


def _preferred(zones: list[str]) -> str:
    """Pick one of several spellings of the same place, deterministically.

    More path segments first, because the specific form is the canonical one
    wherever the pair differs in depth (`America/Argentina/Buenos_Aires` over
    `America/Buenos_Aires`). Then the region rank, then alphabetically so the
    result never depends on set iteration order.
    """
    return min(
        zones,
        key=lambda z: (-z.count("/"), _REGION_RANK.get(z.split("/", 1)[0], 9), z),
    )


@lru_cache(maxsize=1)
def _by_city() -> dict[str, str]:
    """City name -> the one zone to offer for it.

    Built once. `available_timezones()` reads the tz database off disk and
    returns ~600 names; doing that per keystroke of a wizard would be silly.
    """
    grouped: dict[str, list[str]] = {}
    for zone in available_timezones():
        if not zone.startswith(REGIONS):
            continue
        grouped.setdefault(normalise(zone.rsplit("/", 1)[-1]), []).append(zone)
    return {city: _preferred(zones) for city, zones in grouped.items()}


@lru_cache(maxsize=1)
def _exact() -> dict[str, str]:
    """Normalised full zone name -> the zone. The escape hatch for anyone the
    city index fails, and it accepts the legacy names too: someone who types
    `US/Eastern` knows what they mean."""
    return {normalise(z): z for z in available_timezones()}


def resolve_city(text: str) -> list[str]:
    """Candidate IANA zones for what someone typed.

    Returns zero, one, or several. The wizard treats those cases differently
    and this function deliberately does not choose for it. In practice the tz
    database yields at most one after the filtering above, but the list is the
    honest type: an alias like "georgia" is a country and a US state, and the
    day someone asks for that the answer is a question, not a guess.
    """
    key = normalise(text)
    if not key:
        return []

    # A full zone name, typed exactly. Checked first so "Europe/Istanbul" never
    # falls through to the city index.
    if key in _exact():
        return [_exact()[key]]

    if key in ALIASES:
        return [ALIASES[key]]

    found = _by_city().get(key)
    return [found] if found else []
