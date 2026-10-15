"""Translate user interests to documented provider categories, never places."""
import re
import unicodedata


def tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(character for character in normalized if not unicodedata.combining(character))
    return {token for token in re.split(r"[\W_]+", text) if token}

# These are category semantics, not destination or itinerary defaults.
INTEREST_CATEGORIES = {
    "food": ("catering.restaurant", "catering.cafe", "catering.food_court"),
    "nature": ("natural", "leisure.park", "tourism.attraction.viewpoint"),
    "culture": ("entertainment.museum", "entertainment.culture", "tourism.sights", "heritage"),
}
_ALIASES = {
    "food": {"food", "cuisine", "culinary", "dining", "restaurant", "restaurants"},
    "nature": {"nature", "natural", "outdoors", "parks", "park", "hiking"},
    "culture": {"culture", "cultural", "history", "historical", "heritage"},
}


def interest_groups(interests):
    requested = set().union(*(tokens(value) for value in interests)) if interests else set()
    return [group for group, aliases in _ALIASES.items() if requested & aliases]


def matches_interest(attraction, interests):
    literal = tokens(" ".join([attraction.name, *attraction.categories]))
    if any(tokens(value) & literal for value in interests):
        return True
    return any(category == prefix or category.startswith(prefix + ".")
               for group in interest_groups(interests)
               for prefix in INTEREST_CATEGORIES[group]
               for category in attraction.categories)


def place_activity_type(place):
    """Classify named provider POIs; an address/category alone is not a venue."""
    name = place.name.strip()
    if not name or re.fullmatch(r"(?:MDR|NH|SH)\s*[- ]?\d+[A-Z]?", name, re.IGNORECASE):
        return None
    categories = place.categories

    def has_prefix(prefixes):
        return any(category == prefix or category.startswith(prefix + ".")
                   for category in categories for prefix in prefixes)

    if has_prefix(("no_access", "accommodation", "highway", "road")):
        return None
    if has_prefix(("tourism.attraction", "tourism.sights", "tourism.museum",
                   "entertainment", "heritage", "natural", "leisure.park")):
        return "attraction"
    if has_prefix(INTEREST_CATEGORIES["food"]):
        return "meal_break"
    return None
