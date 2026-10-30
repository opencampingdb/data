"""Which kind of place an OSM object is, and which areas can contain a camp site.

The kinds are the OTWD site_types vocabulary plus three Open Camping DB
extensions (schema/kinds.json). farm_stay is never classified from OSM: it
is private land listed by its owner. The rules follow the OTWD OSM mappings. Every
decision, and the order the rules are tried in, is listed in
pipeline/README.md under "Kind".

classify() looks only at an object's own tags, plus the set of container
types it lies inside ("park", "forest", "showground"). Only camp sites use
the containers.
"""

import re

# (key, value) pairs that can make an object a place. The C++ tag filter in
# extract.py lets these through; classify() then decides. Some pairs need a
# second tag as well (shop=laundry needs self_service=yes, for example).
PLACE_TAG_PAIRS = (
    ("tourism", "camp_site"),
    ("tourism", "caravan_site"),
    ("tourism", "picnic_site"),
    ("tourism", "information"),
    ("tourism", "attraction"),
    ("tourism", "viewpoint"),
    ("highway", "rest_area"),
    ("amenity", "sanitary_dump_station"),
    ("amenity", "water_point"),
    ("amenity", "drinking_water"),
    ("amenity", "toilets"),
    ("amenity", "shower"),
    ("amenity", "fuel"),
    ("amenity", "hospital"),
    ("amenity", "ferry_terminal"),
    ("shop", "laundry"),
    ("shop", "gas"),
    ("shop", "car_repair"),
    ("leisure", "swimming_area"),
    ("leisure", "bathing_place"),
    ("leisure", "swimming_pool"),
    ("leisure", "slipway"),
)

# (key, value) pairs that can make an area a container. container_type()
# decides.
CONTAINER_TAG_PAIRS = (
    ("boundary", "national_park"),
    ("boundary", "protected_area"),
    ("landuse", "forest"),
    ("landuse", "recreation_ground"),
    ("leisure", "showground"),
)

# IUCN categories 1a to 6. Categories 7 and up are other kinds of protection
# (water catchments, heritage areas and so on), not parks.
PARK_PROTECT_CLASS = re.compile(r"^(1a|1b|[1-6])$")

# Operators that make a protected area a park whatever its protect_class.
PARK_AGENCY = re.compile(
    r"national parks?|parks (victoria|australia|tasmania|sa)|parks and wildlife|"
    r"parks & wildlife|\bnpws\b|\bqpws\b|\bdbca\b|wildlife service",
    re.IGNORECASE,
)

# Operators that make landuse=forest a state forest: forestry agencies and
# government departments. Private plantation companies do not count.
FOREST_AGENCY = re.compile(
    r"forestry|vicforests|forest products commission|sustainable timber tasmania|"
    r"\bdepartment\b|\bgovernment\b|\bcrown\b",
    re.IGNORECASE,
)

STATE_FOREST = re.compile(r"state forest", re.IGNORECASE)


def _is_yes(value: str | None) -> bool:
    return (value or "").strip().lower() == "yes"


def parse_fee(value: str) -> bool | None:
    """fee=no is free. fee=yes or a price (any value with a digit or $) is paid.

    Anything else, such as donation, is None (not known).
    """
    value = value.strip().lower()
    if value == "no":
        return False
    if value == "yes" or "$" in value or any(c.isdigit() for c in value):
        return True
    return None


def _fee(tags: dict) -> bool | None:
    return parse_fee(tags.get("fee", ""))


def container_type(tags: dict) -> str | None:
    """'park', 'forest' or 'showground' for an area that can contain a camp site, else None.

    A state forest is checked first, so a state forest tagged as a protected
    area (which often carries protect_class 6) is a forest, not a park.
    """
    landuse = tags.get("landuse")
    boundary = tags.get("boundary")
    titles = " ".join(tags.get(k, "") for k in ("name", "official_name", "protection_title"))

    if landuse == "forest" or boundary == "protected_area":
        if STATE_FOREST.search(titles):
            return "forest"
    if landuse == "forest" and FOREST_AGENCY.search(tags.get("operator", "")):
        return "forest"

    if boundary == "national_park":
        return "park"
    if boundary == "protected_area":
        if PARK_PROTECT_CLASS.match(tags.get("protect_class", "").strip().lower()):
            return "park"
        if PARK_AGENCY.search(tags.get("operator", "")):
            return "park"

    if landuse == "recreation_ground" and tags.get("recreation_ground") == "showground":
        return "showground"
    if tags.get("leisure") == "showground":
        return "showground"
    return None


def classify(tags: dict, inside: frozenset = frozenset()) -> str | None:
    """The kind of place for an OSM object's tags, or None if it is not a place we keep.

    inside is the set of container types the place lies in. The rules are
    tried in order and the first that matches wins.
    """
    tourism = tags.get("tourism")
    amenity = tags.get("amenity")
    shop = tags.get("shop")
    leisure = tags.get("leisure")

    # 1. Camp sites: containment first, then the fee.
    if tourism == "camp_site":
        if "park" in inside:
            return "national_park_campsite"
        if "forest" in inside:
            return "state_forest_campsite"
        if "showground" in inside:
            return "showground"
        fee = _fee(tags)
        if fee is False:
            return "free_camp"
        if fee is True:
            return "paid_campground"
        return "campground"

    # 2. Caravan sites. OTWD maps tourism=caravan_site + fee=no to free_camp.
    if tourism == "caravan_site":
        return "free_camp" if _fee(tags) is False else "caravan_park"

    # 3. Utility.
    if tags.get("highway") == "rest_area":
        return "rest_area"
    if amenity == "sanitary_dump_station":
        return "dump_point"
    if amenity in ("water_point", "drinking_water"):
        return "drinking_water_point"
    if amenity == "toilets":
        return "public_toilet"
    if amenity == "shower":
        return "public_shower"
    if shop == "laundry" and _is_yes(tags.get("self_service")):
        return "laundromat"

    # 4. Emergency and transport.
    if amenity == "hospital" and _is_yes(tags.get("emergency")):
        return "hospital_emergency"
    if amenity == "ferry_terminal":
        return "ferry_terminal"

    # 5. Services.
    if amenity == "fuel":
        return "fuel"
    if shop == "gas":
        return "lpg_refill_swap"
    if shop == "car_repair":
        return "mechanic"
    if tourism == "information" and tags.get("information") in ("office", "visitor_centre"):
        return "information_centre"

    # 6. Attractions, the most specific first. tourism=attraction is last
    # because OTWD uses it only when no more specific type fits.
    if tourism == "viewpoint":
        return "lookout"
    if leisure in ("swimming_area", "bathing_place"):
        return "swimming_spot"
    if leisure == "swimming_pool" and tags.get("access", "").strip().lower() in ("yes", "public"):
        return "swimming_spot"
    if leisure == "slipway":
        return "boat_ramp"
    if tourism == "picnic_site":
        return "picnic_site"
    if tourism == "attraction":
        return "attraction"
    return None


def water_type(tags: dict) -> str | None:
    """tank_fill for amenity=water_point, fountain for amenity=drinking_water."""
    amenity = tags.get("amenity")
    if amenity == "water_point":
        return "tank_fill"
    if amenity == "drinking_water":
        return "fountain"
    return None
