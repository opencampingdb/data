"""Which kind of place an OSM object is, and which areas can contain a camp site.

The kinds are the OTWD site_types vocabulary plus three Open Camping DB
extensions (schema/kinds.json). A camp or caravan site whose name says it is
a station or farm stay is a farm_stay (is_farm_stay); owners list the rest. The rules follow the OTWD OSM mappings. Every
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
    ("tourism", "hostel"),
    ("tourism", "motel"),
    ("tourism", "chalet"),
    ("tourism", "museum"),
    ("tourism", "gallery"),
    ("amenity", "parking"),
    ("amenity", "pub"),
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


def parse_fee(value: str) -> bool | str | None:
    """fee=no is free. fee=yes or a price (any value with a digit or $) is paid.
    fee=donation is "donation".

    Anything else is None (not known).
    """
    value = value.strip().lower()
    if value == "no":
        return False
    if value == "donation":
        return "donation"
    if value == "yes" or "$" in value or any(c.isdigit() for c in value):
        return True
    return None


# OSM brand=* values (and park chain names) -> the brand fact. Matched
# without regard to case against the brand tag.
BRANDS = (
    (re.compile(r"\bbig ?4\b", re.IGNORECASE), "big4"),
    (re.compile(r"\bdiscovery\b", re.IGNORECASE), "discovery"),
    (re.compile(r"\bnrma\b", re.IGNORECASE), "nrma"),
    (re.compile(r"\bingenia\b", re.IGNORECASE), "ingenia"),
    (re.compile(r"\breflections\b", re.IGNORECASE), "reflections"),
    (re.compile(r"\bg'?day\b", re.IGNORECASE), "gday"),
    (re.compile(r"\bfamily parks\b", re.IGNORECASE), "family_parks"),
    (re.compile(r"\baspen\b", re.IGNORECASE), "aspen"),
    (re.compile(r"\bkui\b", re.IGNORECASE), "kui"),
    (re.compile(r"\btawk\b", re.IGNORECASE), "tawk"),
    (re.compile(r"\byha\b|youth hostels? association", re.IGNORECASE), "yha"),
    (re.compile(r"\bacc\b|australian camping club", re.IGNORECASE), "acc"),
)

# Brands that are commercial park chains (see is_commercial_park).
PARK_CHAIN_BRANDS = {"aspen", "big4", "discovery", "family_parks", "gday", "ingenia", "kui", "nrma", "reflections"}


def parse_brand(value: str) -> str | None:
    """The brand fact for an OSM brand=* value, or None for a brand we do not list."""
    for pattern, brand in BRANDS:
        if pattern.search(value or ""):
            return brand
    return None


# A station or farm stay names itself so: "Mount Gipps Station", "Bonus Downs
# Farmstay", "Olive Hill Farm". FARM_NOT_STAY holds the words that make such a
# name something else on the map (a pump station, Farm Beach, a wind farm).
FARM_STAY = re.compile(r"\b(farm\s?stay|farm|station)\b", re.IGNORECASE)
FARM_NOT_STAY = re.compile(
    r"\b(pump|pumping|repeater|railway|rail|service|fire|police|ranger|bus|power|weather|research|radio|"
    r"telegraph|petrol|fuel|roadhouse|whaling|checking|logging|payment|tavern|lookout|parking|rest area|"
    r"wind farm|farm beach)\b",
    re.IGNORECASE,
)


def is_farm_stay(tags: dict) -> bool:
    """Whether a camp or caravan site is a station or farm stay, by its name."""
    name = tags.get("name") or ""
    return bool(FARM_STAY.search(name)) and not FARM_NOT_STAY.search(name)


# A holiday park, tourist park, caravan park or resort names itself so, or by
# its chain (Discovery Parks, BIG4, NRMA Parks, Ingenia Holidays, G'day Parks),
# or says so in its website's domain. fee=no on one is a mapping slip (Lane Cove
# River Tourist Park, a paid park, carries it), unless its name says the place
# is free ("Barbour Park Free Caravan Park"). Only the website's domain is
# read: council pages file free camps under paths like /Caravan-Parks-and-Campgrounds/.
COMMERCIAL_PARK = re.compile(
    r"\b(holiday|tourist|caravan)[\s-]?(park|village)s?\b|\bresorts?\b|"
    r"\bdiscovery parks?\b|\bbig ?4\b|\bnrma parks?\b|\bingenia holidays\b|\bg'?day parks?\b",
    re.IGNORECASE,
)
COMMERCIAL_PARK_DOMAIN = re.compile(
    r"(holiday|tourist|caravan)(park|village)|resort|big4|nrmaparks|ingeniaholidays|gdayparks",
    re.IGNORECASE,
)
FREE_IN_NAME = re.compile(r"\bfree", re.IGNORECASE)


def _website_domain(tags: dict) -> str:
    for key in ("website", "contact:website"):
        if key in tags:
            url = tags[key].split(";")[0].strip().lower()
            return re.sub(r"^https?://", "", url).split("/")[0]
    return ""


def is_commercial_park(tags: dict) -> bool:
    """Whether a place's name or website domain says it is a holiday, tourist or caravan park or a resort."""
    name = tags.get("name") or ""
    if FREE_IN_NAME.search(name):
        return False
    if parse_brand(tags.get("brand", "")) in PARK_CHAIN_BRANDS:
        return True
    return bool(COMMERCIAL_PARK.search(name) or COMMERCIAL_PARK_DOMAIN.search(_website_domain(tags)))


def osm_fee(tags: dict) -> bool | None:
    """The fee an object's tags state: parse_fee, except that fee=no on a camp or
    caravan site that is a commercial park is not known. A toilet block in a
    tourist park can be free, so other objects keep fee=no.
    """
    fee = parse_fee(tags.get("fee", ""))
    if (
        fee is False
        and tags.get("tourism") in ("camp_site", "caravan_site")
        and is_commercial_park(tags)
    ):
        return None
    return fee


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
        if is_farm_stay(tags):
            return "farm_stay"
        fee = osm_fee(tags)
        if fee is False:
            return "free_camp"
        if fee is True:
            return "paid_campground"
        return "campground"

    # 2. Caravan sites. OTWD maps tourism=caravan_site + fee=no to free_camp.
    if tourism == "caravan_site":
        if is_farm_stay(tags):
            return "farm_stay"
        return "free_camp" if osm_fee(tags) is False else "caravan_park"

    # 2c. Roofed places to stay. Hotels and guest houses are left out: there
    # are too many, and a caravan park's cabins are the cabins fact.
    if tourism == "hostel":
        return "backpacker_hostel"
    if tourism in ("motel", "chalet"):
        return "roofed_accommodation"

    # 3. Utility.
    if tags.get("highway") == "rest_area":
        return "rest_area"
    if amenity == "parking" and any(
        tags.get(key, "").strip().lower() == "designated" for key in ("motorhome", "caravan", "hgv")
    ):
        return "long_vehicle_parking"
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
    # Pubs only: cafes and restaurants would swamp the camping places.
    if amenity == "pub":
        return "food_and_drink"
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
    if tourism in ("museum", "gallery"):
        return "heritage"
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
