"""Read an OSM file and write the places in it as NDJSON.

Usage:
    python -m pipeline.extract australia-latest.osm.pbf --out extract.ndjson

Reads .osm.pbf or .osm XML with pyosmium. Keeps nodes, closed ways and
multipolygon relations that pipeline.kinds classifies as a place, and
type=site relations with a kind tag. A node is placed at its own position. A
closed way or a multipolygon is placed at the centroid of its area (holes
taken out). A site relation is placed at the mean of its members' positions.
An open way that is a boat ramp or an attraction (slipways and walls are
often drawn as lines) is placed at its middle vertex. Other relations and
open ways are skipped.

Camp sites are classified by the areas they lie in: national parks and other
parks, state forests and showgrounds. Those areas are built in the same read
and tested with an STRtree (shapely).

Each output line is one place:
    {"osm": {...}, "kind": "...", "name": ..., "location": {...}, "facts": {...}}
osm holds type, id, version and, when the file carries it, the timestamp of
that version, and check_dates when the object has check_date:charge or
check_date:charge:conditional.
sorted by OSM type and then id, so the same input always gives the same file.
The tag mappings are documented in pipeline/README.md.
"""

import argparse
import re
import sys
from datetime import timezone
from pathlib import Path

import osmium
import shapely
from shapely import MultiPolygon, Polygon, STRtree

from .common import KIND_GROUP, dump_line, pets_months_ok, round_coord, write_text
from .geometry import SidecarBuilder
from .kinds import (
    CONTAINER_TAG_PAIRS,
    PLACE_TAG_PAIRS,
    classify,
    container_type,
    osm_fee,
    parse_brand,
    water_type,
)
from .localities import LOCALITY_TAG_PAIRS, Localities, is_lga, locality_type, write_localities
from .nearby import NEARBY_TAG_PAIRS, InsideFeatures, inside_fact
from .roads import MajorRoads
from .water import WATER_TAG_PAIRS, WaterFeatures, crocodile_hazard, open_line, water_kinds

# Simple yes/no tags: OSM key -> fact key.
YES_NO_TAGS = {
    "power_supply": "power",
    "tents": "tents",
    "caravans": "caravans",
    "toilets": "toilets",
    "drinking_water": "drinking_water",
    "sanitary_dump_station": "dump_station",
    "wheelchair": "wheelchair",
    "motorhome": "motorhomes",
    "washing_machine": "laundry",
    "kitchen": "kitchen",
    "bbq": "bbq",
    "picnic_table": "picnic_tables",
    "openfire": "campfires",
    "cabins": "cabins",
    "playground": "playground",
    "bin": "bins",
    "compressed_air": "air",
    "fishing": "fishing",
}

# Fuels sold: OSM fuel:* key -> the name in the fuels fact, in the order written.
# fuel:lpg is LPG at the pump, so it feeds fuels only, never the lpg fact
# (gas bottle refill or swap).
FUEL_TAGS = {
    "fuel:diesel": "diesel",
    "fuel:octane_91": "unleaded_91",
    "fuel:e10": "e10",
    "fuel:octane_95": "unleaded_95",
    "fuel:octane_98": "unleaded_98",
    "fuel:e85": "e85",
    "fuel:adblue": "adblue",
    "fuel:lpg": "lpg",
}

# toilets:disposal -> toilet_type.
TOILET_TYPES = {"flush": "flush", "pitlatrine": "pit", "chemical": "chemical", "dry_toilet": "dry", "bucket": "dry"}

OPERATOR_MAX = 200
PHONE_MAX = 100
URL_MAX = 500
MAX_STAY_MAX = 100

# Social pages: fact key -> OSM keys, the first with a value wins. Only
# https links are kept.
SOCIAL_TAGS = {
    "facebook": ("contact:facebook", "facebook"),
    "instagram": ("contact:instagram", "instagram"),
    "tiktok": ("contact:tiktok", "tiktok"),
    "x": ("contact:twitter", "contact:x", "twitter"),
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Street address: address field -> OSM keys, the first with a value wins.
# addr:city is the suburb or town in Australian OSM data.
ADDRESS_TAGS = {
    "unit": ("addr:unit",),
    "housenumber": ("addr:housenumber",),
    "street": ("addr:street",),
    "place": ("addr:place",),
    "suburb": ("addr:suburb", "addr:city"),
    "state": ("addr:state",),
    "postcode": ("addr:postcode",),
}
ADDRESS_MAX = 100
POSTCODE_RE = re.compile(r"^\d{4}$")
STATES = {
    "nsw": "NSW", "new south wales": "NSW",
    "vic": "VIC", "victoria": "VIC",
    "qld": "QLD", "queensland": "QLD",
    "sa": "SA", "south australia": "SA",
    "wa": "WA", "western australia": "WA",
    "tas": "TAS", "tasmania": "TAS",
    "nt": "NT", "northern territory": "NT",
    "act": "ACT", "australian capital territory": "ACT",
}

# maxstay units we can read, and the short unit we write.
MAXSTAY_UNITS = {
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "d": "d", "day": "d", "days": "d", "night": "d", "nights": "d",
    "w": "w", "wk": "w", "wks": "w", "week": "w", "weeks": "w",
    "mo": "mo", "month": "mo", "months": "mo",
}
MAXSTAY_RE = re.compile(r"^(\d+)\s*([a-z]+)$")

# OSM access=* -> who may stay (the "entry" fact).
ENTRY_VALUES = {
    "yes": "public",
    "permissive": "public",
    "public": "public",
    "customers": "customers",
    "permit": "permit",
    "private": "private",
    "no": "private",
}


def first_value(value: str) -> str:
    """OSM allows several values separated by ';'. Keep the first."""
    return value.split(";")[0].strip()


def kept_url(url: str, schemes: tuple[str, ...] = ("https://",)) -> bool:
    """Whether a link starts with one of schemes and fits the place schema's URL_MAX."""
    return url.startswith(schemes) and len(url) <= URL_MAX


def yes_no(value: str | None) -> bool | None:
    """'yes' -> True, 'no' -> False, anything else -> None (not stated)."""
    if value is None:
        return None
    value = value.strip().lower()
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def parse_maxstay(value: str) -> str:
    """Shorten common maxstay forms ('48 hours' -> '48h'). Keep anything else as written."""
    value = value.strip()
    match = MAXSTAY_RE.match(value.lower())
    if match and match.group(2) in MAXSTAY_UNITS:
        return match.group(1) + MAXSTAY_UNITS[match.group(2)]
    return value


def parse_address(tags: dict) -> dict | None:
    """The street address in addr:* tags, or None without a street or a place name.

    State is written as its abbreviation and postcode only as four digits;
    other values are kept as written, trimmed.
    """
    address = {}
    for field, osm_keys in ADDRESS_TAGS.items():
        for key in osm_keys:
            value = tags.get(key, "").strip()
            if value:
                if len(value) <= ADDRESS_MAX:
                    address[field] = value
                break
    if "state" in address:
        state = STATES.get(address["state"].lower())
        if state:
            address["state"] = state
        else:
            del address["state"]
    if "postcode" in address and not POSTCODE_RE.match(address["postcode"]):
        del address["postcode"]
    if "street" not in address and "place" not in address:
        return None
    return address


# OSM charge tags, copied as written, and the facts they fill.
CHARGE_TAGS = {"charge": "charge", "charge:conditional": "charge_conditional"}
CHECK_DATE_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")

DOG_MONTHS_RE = re.compile(r"^yes\s*@\s*\(?\s*([^()]+?)\s*\)?$", re.IGNORECASE)


def dog_months(conditional: str) -> str | None:
    """The months of dog:conditional=yes @ (May-Sep), as "May-Sep", or None.

    Only a single yes condition of one range of two different months counts:
    a single month, a list, times, weekdays, other values and several
    conditions are left out.
    """
    match = DOG_MONTHS_RE.match(conditional.strip())
    if not match:
        return None
    months = re.sub(r"\s*-\s*", "-", match.group(1)).title()
    return months if pets_months_ok(months) else None


def heavy_vehicles_only(tags: dict) -> bool:
    """True when OSM says only heavy vehicles may use the place.

    hgv=designated with motorcar=no or motor_vehicle=no, or access=no with
    hgv=yes or hgv=designated.
    """
    value = {key: first_value(tags.get(key, "")).lower() for key in ("hgv", "motorcar", "motor_vehicle", "access")}
    if value["hgv"] == "designated" and "no" in (value["motorcar"], value["motor_vehicle"]):
        return True
    return value["access"] == "no" and value["hgv"] in ("yes", "designated")


# Access values that let motorbikes in, and the one that keeps them out.
MOTORBIKE_ALLOWED = {"yes": True, "designated": True, "permissive": True, "no": False}


def motorbikes_allowed(tags: dict) -> bool | None:
    """Whether motorbikes may enter: a permission, not whether the road suits one.

    The OSM wiki's Key:motorcycle is "Access permission for motorcycles", and
    the tourism=camp_site page's motor_vehicle row says "Whether motor vehicles
    (motorcycles, cars, trucks, caravans) are allowed". motorcycle is read
    first. Only when it is absent does motor_vehicle answer, so a walk-in site
    tagged motor_vehicle=no gets False. Other values, such as private or
    destination, are left out, and a motorcycle value left out is not
    overridden by motor_vehicle.
    """
    for key in ("motorcycle", "motor_vehicle"):
        value = first_value(tags.get(key, "")).lower()
        if value:
            return MOTORBIKE_ALLOWED.get(value)
    return None


def facts_from_tags(tags: dict) -> dict:
    """Map OSM tags to the facts in a place record. Unknown values are left out."""
    facts = {}

    fee = osm_fee(tags)
    if fee is not None:
        facts["fee"] = fee

    for osm_key, fact_key in YES_NO_TAGS.items():
        value = yes_no(tags.get(osm_key))
        if value is not None:
            facts[fact_key] = value

    shower = tags.get("shower", "").strip().lower()
    if shower in ("yes", "hot", "cold"):
        facts["showers"] = True
    elif shower == "no":
        facts["showers"] = False

    # An explicit hot_water tag wins. shower=hot or cold only fills the gap.
    hot_water = yes_no(tags.get("hot_water"))
    if hot_water is None and shower in ("hot", "cold"):
        hot_water = shower == "hot"
    if hot_water is not None:
        facts["hot_water"] = hot_water

    fuels = [name for osm_key, name in FUEL_TAGS.items() if yes_no(tags.get(osm_key))]
    if fuels:
        facts["fuels"] = fuels

    reservation = first_value(tags.get("reservation", "")).lower()
    if reservation == "required":
        facts["booking_required"] = True
    elif reservation == "no":
        facts["booking_required"] = False

    toilet_type = TOILET_TYPES.get(first_value(tags.get("toilets:disposal", "")).lower())
    if toilet_type:
        facts["toilet_type"] = toilet_type

    operator = tags.get("operator", "").strip()
    if operator and len(operator) <= OPERATOR_MAX:
        facts["operator"] = operator

    dog = tags.get("dog", "").strip().lower()
    if dog in ("yes", "leashed", "no"):
        facts["pets"] = dog
    months = dog_months(tags.get("dog:conditional", ""))
    if months and dog in ("", "no"):
        facts["pets"] = "seasonal"
        facts["pets_months"] = months

    if tags.get("maxstay", "").strip():
        max_stay = parse_maxstay(tags["maxstay"])
        if len(max_stay) <= MAX_STAY_MAX:
            facts["max_stay"] = max_stay

    if tags.get("opening_hours", "").strip():
        facts["opening_hours"] = tags["opening_hours"].strip()

    # Charges are kept as OSM has them, out-of-spec values too (ADR 0002).
    for osm_key, fact_key in CHARGE_TAGS.items():
        value = tags.get(osm_key, "").strip()
        if value:
            facts[fact_key] = value[:255]

    # website wins over contact:website. Only http and https links are kept,
    # with the scheme in lower case as the place schema writes it.
    for key in ("website", "contact:website"):
        if key in tags:
            url = first_value(tags[key])
            if kept_url(url, ("http://", "https://")):
                facts["website"] = url
            break

    # website:booking wins over reservation:website. Only https links are kept.
    for key in ("website:booking", "reservation:website"):
        url = first_value(tags.get(key, ""))
        if url:
            if kept_url(url):
                facts["booking_url"] = url
            break

    for key in ("phone", "contact:phone"):
        if key in tags and first_value(tags[key]):
            numbers = tags[key].split(";")
            first = first_value(tags[key])
            if len(first) > PHONE_MAX:
                break
            facts["phone"] = first
            # The second number, from the same key, is phone_2, unless it repeats the first.
            second = numbers[1].strip() if len(numbers) > 1 else ""
            if second and second != first and len(second) <= PHONE_MAX:
                facts["phone_2"] = second
            break

    for key in ("email", "contact:email"):
        if key in tags:
            email = first_value(tags[key])
            if EMAIL_RE.match(email) and len(email) <= 254:
                facts["email"] = email
            break

    for fact_key, osm_keys in SOCIAL_TAGS.items():
        for key in osm_keys:
            if key in tags:
                url = first_value(tags[key])
                if kept_url(url):
                    facts[fact_key] = url
                break

    internet = first_value(tags.get("internet_access", "")).lower()
    if internet in ("wlan", "yes", "wifi"):
        facts["wifi"] = True
    elif internet == "no":
        facts["wifi"] = False

    nudism = first_value(tags.get("nudism", "")).lower()
    if nudism in ("yes", "designated", "customary"):
        facts["nudist"] = True
    elif nudism == "no":
        facts["nudist"] = False

    if first_value(tags.get("min_age", "")) in ("50", "55", "60"):
        facts["over_50s"] = True

    address = parse_address(tags)
    if address:
        facts["address"] = address

    brand = parse_brand(tags.get("brand", ""))
    if brand:
        facts["brand"] = brand

    four_wd = tags.get("4wd_only", "").strip().lower()
    if four_wd == "yes":
        facts["access"] = "4wd"
    elif four_wd == "recommended":
        facts["access"] = "awd"
    elif four_wd == "no":
        facts["access"] = "2wd"

    if heavy_vehicles_only(tags):
        facts["vehicle_access"] = "heavy_vehicles_only"

    if crocodile_hazard(tags):
        facts["crocodiles"] = True

    motorbikes = motorbikes_allowed(tags)
    if motorbikes is not None:
        facts["motorbikes"] = motorbikes

    # access=* says who may enter. It maps to "entry", not to "access", which
    # is about the road. access=no for a heavy vehicle place says who may
    # drive in, not that it is private.
    entry = ENTRY_VALUES.get(first_value(tags.get("access", "")).lower())
    if entry and not ("vehicle_access" in facts and entry == "private" and first_value(tags.get("access", "")).lower() == "no"):
        facts["entry"] = entry

    return facts


def ring_area_and_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Area and centroid of one closed ring of (lon, lat) points, using the shoelace formula.

    Degrees are treated as flat x and y, which is close enough over the size
    of a camp ground. Points are shifted to start at the first point so that
    large coordinates do not cost precision. Returns (area, lon, lat) with
    the area always positive.
    """
    x0, y0 = ring[0]
    twice_area = cx = cy = 0.0
    for (xa, ya), (xb, yb) in zip(ring, ring[1:]):
        xa, ya, xb, yb = xa - x0, ya - y0, xb - x0, yb - y0
        cross = xa * yb - xb * ya
        twice_area += cross
        cx += (xa + xb) * cross
        cy += (ya + yb) * cross
    if twice_area == 0:
        return 0.0, x0, y0
    return abs(twice_area) / 2, x0 + cx / (3 * twice_area), y0 + cy / (3 * twice_area)


def area_centroid(polygons: list[tuple[list, list[list]]]) -> tuple[float, float] | None:
    """Centroid (lat, lon) of polygons given as (outer ring, [inner rings]).

    Each ring is a closed list of (lon, lat) points. Holes count as negative
    area. If the total area is zero (a degenerate shape), the mean of the
    outer ring points is used instead.
    """
    total = sum_x = sum_y = 0.0
    for outer, inners in polygons:
        for ring, sign in [(outer, 1)] + [(inner, -1) for inner in inners]:
            area, x, y = ring_area_and_centroid(ring)
            total += sign * area
            sum_x += sign * area * x
            sum_y += sign * area * y
    if total > 0:
        return sum_y / total, sum_x / total
    points = [p for outer, _ in polygons for p in outer[:-1]]
    if not points:
        return None
    return sum(p[1] for p in points) / len(points), sum(p[0] for p in points) / len(points)


def ring_points(node_refs) -> list[tuple[float, float]] | None:
    """(lon, lat) points of a ring, or None if any node location is missing."""
    points = []
    for ref in node_refs:
        if not ref.location.valid():
            return None
        points.append((ref.location.lon, ref.location.lat))
    return points


def way_centroid(way) -> tuple[float, float] | None:
    """Centroid of a closed way's area, or None if a node location is missing."""
    ring = ring_points(way.nodes)
    if ring is None or len(ring) < 2:
        return None
    return area_centroid([(ring, [])])


def relation_area_centroid(area) -> tuple[float, float] | None:
    """Centroid of a multipolygon area built by pyosmium, or None if a location is missing."""
    polygons = []
    for outer in area.outer_rings():
        outer_points = ring_points(outer)
        if outer_points is None:
            return None
        inners = []
        for inner in area.inner_rings(outer):
            inner_points = ring_points(inner)
            if inner_points is None:
                return None
            inners.append(inner_points)
        polygons.append((outer_points, inners))
    if not polygons:
        return None
    return area_centroid(polygons)


# Kinds that are often mapped as an open line, such as a slipway drawn down
# into the water. Such a way is placed at its middle vertex. Open ways of
# every other kind are skipped.
OPEN_WAY_KINDS = ("boat_ramp", "attraction")


def interesting_tag_filter() -> "osmium.filter.TagFilter":
    """A C++ filter that lets through objects that may be a place, a container or a nearby feature.

    A filter object belongs to one handler chain, so each chain gets its own.
    """
    return osmium.filter.TagFilter(
        *PLACE_TAG_PAIRS, *CONTAINER_TAG_PAIRS, *WATER_TAG_PAIRS, *NEARBY_TAG_PAIRS, *LOCALITY_TAG_PAIRS
    )


def tag_dict(obj) -> dict:
    return {tag.k: tag.v for tag in obj.tags}


def osm_timestamp(obj) -> str | None:
    """When the object's current version was saved in OSM, as ISO 8601 UTC.

    None when the file does not carry timestamps: pyosmium then reports the
    Unix epoch. Geofabrik extracts and Overpass "out meta" both carry them.
    """
    stamp = obj.timestamp
    if stamp is None or stamp.timestamp() <= 0:
        return None
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_dates(tags: dict) -> dict:
    """check_date:<tag> for the charge facts, by fact, when the tag itself is set and the date is YYYY-MM-DD."""
    dates = {}
    for osm_key, fact_key in CHARGE_TAGS.items():
        date = tags.get(f"check_date:{osm_key}", "").strip()
        if tags.get(osm_key, "").strip() and CHECK_DATE_RE.match(date):
            dates[fact_key] = date
    return dates


def osm_link(osm_type: str, osm_id: int, version: int, timestamp: str | None, tags: dict | None = None) -> dict:
    """The record's osm object. timestamp is left out when the file has none, check_dates when no tag has one."""
    link = {"type": osm_type, "id": osm_id, "version": version}
    if timestamp is not None:
        link["timestamp"] = timestamp
    dates = check_dates(tags or {})
    if dates:
        link["check_dates"] = dates
    return link


class SiteRelations:
    """type=site relations that are places, and the positions of their members.

    A site relation groups the parts of one place (a camp ground's entrance
    node, its outline way, its toilet block) without being an area itself. It
    is placed at the mean of its members' positions: a node's location, a
    way's centroid (the area centroid of a closed way, the mean of the nodes
    of an open one) and the centroid of a member multipolygon's area. Nested
    site relations are not followed.
    """

    def __init__(self) -> None:
        self.sites: dict[int, dict] = {}
        self.member_nodes: set[int] = set()
        self.member_ways: set[int] = set()
        self.member_relations: set[int] = set()
        self.positions: dict[tuple[str, int], tuple[float, float]] = {}

    def relation(self, relation) -> None:
        tags = tag_dict(relation)
        if tags.get("type") != "site" or classify(tags) is None:
            return
        members = [(m.type, m.ref) for m in relation.members]
        self.sites[relation.id] = {
            "tags": tags,
            "version": relation.version,
            "timestamp": osm_timestamp(relation),
            "members": members,
        }
        for member_type, ref in members:
            if member_type == "n":
                self.member_nodes.add(ref)
            elif member_type == "w":
                self.member_ways.add(ref)
            elif member_type == "r":
                self.member_relations.add(ref)

    def note_way(self, way) -> None:
        """Second pass: note the position of a member way."""
        if way.id not in self.member_ways:
            return
        points = ring_points(way.nodes)
        if not points:
            return
        if way.is_closed() and len(points) >= 4:
            position = way_centroid(way)
        else:
            position = (
                sum(p[1] for p in points) / len(points),
                sum(p[0] for p in points) / len(points),
            )
        if position is not None:
            self.positions[("w", way.id)] = position

    def note_area(self, area) -> None:
        if not area.from_way() and area.orig_id() in self.member_relations:
            position = relation_area_centroid(area)
            if position is not None:
                self.positions[("r", area.orig_id())] = position

    def note_nodes(self, location_store) -> None:
        for node_id in self.member_nodes:
            try:
                location = location_store.get(node_id)
            except KeyError:
                continue
            if location.valid():
                self.positions[("n", node_id)] = (location.lat, location.lon)

    def position(self, relation_id: int) -> tuple[float, float] | None:
        """Mean of the positions of the members that could be placed, or None."""
        found = [
            self.positions[key]
            for key in self.sites[relation_id]["members"]
            if key in self.positions
        ]
        if not found:
            return None
        return (
            sum(lat for lat, _ in found) / len(found),
            sum(lon for _, lon in found) / len(found),
        )


class SiteWayCollector:
    """Second-pass handler that hands ways to SiteRelations. Passes every object on."""

    def __init__(self, sites: SiteRelations) -> None:
        self.sites = sites

    def way(self, way) -> None:
        self.sites.note_way(way)


class FirstPass:
    """First pass: finds the ways the main pass must keep.

    Sees the ways that passed the tag filter, and every relation. Records
    the closed ways that are a place or a container, and the member ways of
    multipolygon and boundary relations that are a place or a container or a
    member of a site relation that is a place. Every other way, buildings
    included, is kept away from the area builder.

    relation() returns True for a relation the area builder must not
    assemble, which drops it from the rest of the handler chain.
    """

    def __init__(self, stats: dict, sites: SiteRelations) -> None:
        self.way_ids: set[int] = set(sites.member_ways)
        self.stats = stats
        self.sites = sites

    def way(self, way) -> None:
        tags = tag_dict(way)
        kind = classify(tags)
        if water_kinds(tags):
            # Beaches drawn as open lines are kept too, as lines.
            self.way_ids.add(way.id)
        if inside_fact(tags) and way.is_closed():
            # A car wash drawn as an area is placed at its centroid.
            self.way_ids.add(way.id)
        if kind is None and container_type(tags) is None:
            return
        if way.is_closed() or kind in OPEN_WAY_KINDS:
            self.way_ids.add(way.id)
        elif kind is not None:
            self.stats["open_ways"] += 1

    def relation(self, relation) -> bool:
        tags = tag_dict(relation)
        if tags.get("type") not in ("multipolygon", "boundary"):
            return True
        wanted = relation.id in self.sites.member_relations
        if (
            not wanted
            and classify(tags) is None
            and container_type(tags) is None
            and not water_kinds(tags)
            and inside_fact(tags) is None
            and not is_lga(tags)
        ):
            return True
        for member in relation.members:
            if member.type == "w":
                self.way_ids.add(member.ref)
        return False


class AreaWayCounter:
    """Counts the ways handed to the area builder. Passes every object on."""

    def __init__(self, stats: dict) -> None:
        self.stats = stats

    def way(self, way) -> None:
        self.stats["ways_to_area_builder"] += 1


def read_objects(path: Path, stats: dict, sites: SiteRelations, roads: MajorRoads):
    """Yield every OSM object or area that may be a place or a container.

    Fills sites with the site relations that are places and the positions of
    their members.

    Three passes over the file:
    0. Relations only: find the site relations that are places, and their
       members (SiteRelations).
    1. Ways and relations only, no locations: collect the ways that matter
       (FirstPass), and let the area builder take note of the relations
       FirstPass keeps.
    2. Everything, with a node location cache. A way id filter sits in front
       of the area builder, so it never sees a way it does not need. Only
       objects with an interesting tag come out at the end. Every way passes
       the major road check (pipeline.roads) first, before the filter.

    pyosmium's FileProcessor.with_areas() filters only the relations, and
    would assemble an area for every closed way in the file, so this chain
    is built by hand.
    """
    # Every native object is held in a local variable for the whole read:
    # the handlers keep only pointers to each other, so an object that
    # Python frees early crashes the reader.
    site_filter = osmium.filter.TagFilter(*PLACE_TAG_PAIRS)
    with osmium.io.Reader(str(path), osmium.osm.RELATION) as reader:
        osmium.apply(reader, site_filter, sites)

    area_manager = osmium.area.AreaManager()
    first = FirstPass(stats, sites)
    # The tag filter applies to ways only. Every relation reaches FirstPass,
    # which keeps untagged multipolygons that are members of a site.
    first_filter = interesting_tag_filter()
    first_filter.enable_for(osmium.osm.WAY)
    first_pass = area_manager.first_pass_handler()
    with osmium.io.Reader(str(path), osmium.osm.WAY | osmium.osm.RELATION) as reader:
        osmium.apply(reader, first_filter, first, first_pass)

    area_gate = osmium.filter.IdFilter(first.way_ids)
    area_gate.enable_for(osmium.osm.WAY)
    # Areas are not filtered by tag, so that untagged multipolygons that are
    # members of a site come out. Only gated ways and kept relations are
    # assembled, so there are few.
    areas = osmium.BufferIterator()
    second_pass = area_manager.second_pass_to_buffer(areas)
    counter = AreaWayCounter(stats)
    site_ways = SiteWayCollector(sites)
    output_filter = interesting_tag_filter()
    location_store = osmium.index.create_map("flex_mem")
    locations = osmium.NodeLocationsForWays(location_store)
    locations.ignore_errors()

    with osmium.io.Reader(str(path)) as reader:
        objects = osmium.OsmFileIterator(
            reader, locations, roads, area_gate, site_ways, counter, second_pass, output_filter
        )
        for obj in objects:
            yield obj
            if areas:
                yield from yield_areas(areas, sites)
    if areas:
        yield from yield_areas(areas, sites)
    sites.note_nodes(location_store)


def yield_areas(areas, sites: SiteRelations):
    for area in areas:
        sites.note_area(area)
        yield area


def object_position(obj, stats: dict, kind: str | None = None) -> tuple[str, int, float, float] | None:
    """(osm type, osm id, lat, lon) of a node, closed way or multipolygon area.

    None for relations (used through their areas), for areas built from
    ways (the way itself is used), for open ways and when a node location is
    missing.
    """
    if obj.is_node():
        if not obj.location.valid():
            stats["missing_locations"] += 1
            return None
        return "node", obj.id, obj.location.lat, obj.location.lon
    if obj.is_relation():
        return None
    if obj.is_area():
        if obj.from_way():
            return None
        centroid = relation_area_centroid(obj)
        if centroid is None:
            stats["missing_locations"] += 1
            return None
        return "relation", obj.orig_id(), *centroid
    if not obj.is_closed():
        if kind not in OPEN_WAY_KINDS:
            return None  # already counted in the first pass
        nodes = list(obj.nodes)
        middle = nodes[len(nodes) // 2] if nodes else None
        if middle is None or not middle.location.valid():
            stats["missing_locations"] += 1
            return None
        stats["open_way_places"] += 1
        return "way", obj.id, middle.location.lat, middle.location.lon
    centroid = way_centroid(obj)
    if centroid is None:
        stats["missing_locations"] += 1
        return None
    return "way", obj.id, *centroid


def container_geometry(obj):
    """A shapely polygon for a closed way or multipolygon area, or None."""
    if obj.is_area():
        if obj.from_way():
            return None
        polygons = []
        for outer in obj.outer_rings():
            outer_points = ring_points(outer)
            if outer_points is None or len(outer_points) < 4:
                return None
            inners = [ring_points(inner) for inner in obj.inner_rings(outer)]
            polygons.append(Polygon(outer_points, [i for i in inners if i and len(i) >= 4]))
        geometry = MultiPolygon(polygons) if polygons else None
    elif obj.is_way() and obj.is_closed():
        points = ring_points(obj.nodes)
        geometry = Polygon(points) if points and len(points) >= 4 else None
    else:
        return None
    if geometry is not None and not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    return geometry


def containers_for(points: list[tuple[float, float]], containers: list[tuple]) -> list[set[str]]:
    """For each (lat, lon) point, the set of container types it lies in."""
    found: list[set[str]] = [set() for _ in points]
    if not points or not containers:
        return found
    tree = STRtree([geometry for geometry, _ in containers])
    query = shapely.points([(lon, lat) for lat, lon in points])
    point_index, container_index = tree.query(query, predicate="intersects")
    for p, c in zip(point_index, container_index):
        found[p].add(containers[c][1])
    return found


def water_geometry(obj):
    """A shapely geometry for a water feature: a point, an area or, for an open way, a line."""
    if obj.is_node():
        return shapely.Point(obj.location.lon, obj.location.lat) if obj.location.valid() else None
    if obj.is_way() and not obj.is_closed():
        return open_line(obj)
    return container_geometry(obj)


def feature_key(obj) -> tuple[str, int]:
    """(osm type, osm id) of a node, way or multipolygon area, as a place record names it."""
    if obj.is_node():
        return "node", obj.id
    if obj.is_area():
        return ("way" if obj.from_way() else "relation"), obj.orig_id()
    return "way", obj.id


def feature_point(obj) -> tuple[float, float] | None:
    """(lat, lon) of a node, or the centroid of a closed way or multipolygon area.

    None for open ways, for areas built from ways (the way itself is used)
    and when a node location is missing.
    """
    if obj.is_node():
        return (obj.location.lat, obj.location.lon) if obj.location.valid() else None
    if obj.is_area():
        return None if obj.from_way() else relation_area_centroid(obj)
    if obj.is_way() and obj.is_closed():
        return way_centroid(obj)
    return None


def place_outline(obj):
    """The outline of a place that is an area (a closed way or multipolygon), or None."""
    if obj.is_area() and obj.from_way():
        return None
    return container_geometry(obj)


def extract_file(
    path: Path,
    stats: dict | None = None,
    sidecar: SidecarBuilder | None = None,
    localities: Localities | None = None,
) -> list[dict]:
    """Read an OSM file and return the place records found in it, sorted.

    If a dict is passed as stats it is filled with counts of what was kept
    and skipped, of the ways handed to the area builder and of the
    containers found, for the log. A SidecarBuilder passed as sidecar
    collects the outlines, signals and major roads for pipeline.geometry.
    A Localities passed as localities keeps the towns and council areas read,
    for the localities file (pipeline.localities).
    """
    stats = stats if stats is not None else {}
    stats.update(
        kept=0, open_ways=0, open_way_places=0, missing_locations=0, ways_to_area_builder=0
    )
    container_counts = {"park": 0, "reserve": 0, "forest": 0, "showground": 0}

    places: list[tuple[dict, dict]] = []  # (record without kind, tags)
    outlines: dict[int, object] = {}  # index in places -> outline of a place that is an area
    containers: list[tuple] = []  # (shapely geometry, container type)
    sites = SiteRelations()
    water = WaterFeatures()
    inside = InsideFeatures()
    roads = MajorRoads()
    localities = localities if localities is not None else Localities()
    if sidecar is not None:
        # The sidecar writes its road lines from the same roads.
        sidecar.roads = roads.roads

    for obj in read_objects(path, stats, sites, roads):
        tags = tag_dict(obj)

        kinds_of_water = water_kinds(tags)
        if kinds_of_water:
            geometry = water_geometry(obj)
            if geometry is not None:
                water.add(kinds_of_water, geometry, tags.get("name"), feature_key(obj))

        fact_inside = inside_fact(tags)
        if fact_inside is not None:
            point = feature_point(obj)
            if point is not None:
                inside.add(fact_inside, *point, feature_key(obj))

        if obj.is_node() and locality_type(tags) and obj.location.valid():
            localities.add_town(obj.id, tags, obj.location.lat, obj.location.lon)
        if obj.is_area() and not obj.from_way() and is_lga(tags):
            localities.add_lga(obj.orig_id(), tags, container_geometry(obj))

        kind_of_container = container_type(tags)
        if kind_of_container is not None:
            geometry = container_geometry(obj)
            if geometry is not None:
                containers.append((geometry, kind_of_container))
                container_counts[kind_of_container] += 1

        if classify(tags) is None:
            continue
        position = object_position(obj, stats, classify(tags))
        if position is None:
            continue
        osm_type, osm_id, lat, lon = position
        record = {
            "osm": osm_link(osm_type, osm_id, obj.version, osm_timestamp(obj), tags),
            "name": tags.get("name", "").strip() or None,
            "location": {"lat": round_coord(lat), "lon": round_coord(lon)},
            "facts": facts_from_tags(tags),
        }
        outline = place_outline(obj) if osm_type != "node" else None
        if outline is not None:
            outlines[len(places)] = outline
        places.append((record, tags))
        if sidecar is not None:
            sidecar.place(f"{osm_type}/{osm_id}", tags, outline)

    stats["site_relations"] = 0
    for relation_id, site in sorted(sites.sites.items()):
        position = sites.position(relation_id)
        if position is None:
            stats["missing_locations"] += 1
            continue
        tags = site["tags"]
        record = {
            "osm": osm_link("relation", relation_id, site["version"], site["timestamp"], tags),
            "name": tags.get("name", "").strip() or None,
            "location": {"lat": round_coord(position[0]), "lon": round_coord(position[1])},
            "facts": facts_from_tags(tags),
        }
        places.append((record, tags))
        stats["site_relations"] += 1
        if sidecar is not None:
            sidecar.place(f"relation/{relation_id}", tags, None)

    # Only camp sites depend on the areas they lie in.
    camp_indexes = [i for i, (_, tags) in enumerate(places) if tags.get("tourism") == "camp_site"]
    camp_points = [
        (places[i][0]["location"]["lat"], places[i][0]["location"]["lon"]) for i in camp_indexes
    ]
    inside_by_index = dict(zip(camp_indexes, containers_for(camp_points, containers)))

    records = []
    for i, (record, tags) in enumerate(places):
        record["kind"] = classify(tags, frozenset(inside_by_index.get(i, ())))
        if record["kind"] == "drinking_water_point":
            record["facts"]["water_type"] = water_type(tags)
        # A dump station for customers is one a guest may use, so it counts
        # on a place to stay. On any other kind it is not open to everyone.
        if KIND_GROUP.get(record["kind"]) == "overnight" and first_value(tags.get("sanitary_dump_station", "")).lower() == "customers":
            record["facts"]["dump_station"] = True
        key = (record["osm"]["type"], record["osm"]["id"])
        water.set_facts(record, key)
        inside.set_facts(record, outlines.pop(i, None), key)
        roads.set_facts(record)
        records.append(record)
    localities.set_facts(records)

    stats["kept"] = len(records)
    stats["containers"] = container_counts
    stats["water"] = {kind: water.count(kind) for kind in ("beach", "crocodiles", "boat_ramp")}
    stats["inside"] = {fact: inside.count(fact) for fact in ("pub", "fuel", "car_wash")}
    stats["major_roads"] = roads.count()
    stats["localities"] = localities.counts()
    records.sort(key=lambda r: (r["osm"]["type"], r["osm"]["id"]))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help=".osm.pbf or .osm file")
    parser.add_argument("--out", type=Path, default=Path("extract.ndjson"))
    parser.add_argument("--geometry", type=Path, help="also write the outlines, signals and major roads (pipeline.geometry) here")
    parser.add_argument("--localities", type=Path, help="also write every town and its council area (pipeline.localities) here")
    args = parser.parse_args(argv)

    stats: dict = {}
    sidecar = SidecarBuilder() if args.geometry else None
    localities = Localities()
    records = extract_file(args.input, stats, sidecar, localities)
    write_text(args.out, "".join(dump_line(r) + "\n" for r in records))
    if args.localities:
        write_localities(args.localities, localities.rows())
    if sidecar is not None:
        lines = sidecar.write(args.geometry, records)
        print(f"Wrote {lines} geometry lines to {args.geometry} ({len(sidecar.roads)} major roads read)", file=sys.stderr)

    by_kind: dict[str, int] = {}
    for record in records:
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
    print(f"Kept {stats['kept']} places in {args.out}", file=sys.stderr)
    for kind in sorted(by_kind):
        print(f"  {kind}: {by_kind[kind]}", file=sys.stderr)
    print(
        f"Skipped {stats['open_ways']} open ways and "
        f"{stats['missing_locations']} objects with missing node locations",
        file=sys.stderr,
    )
    print(f"Placed {stats['open_way_places']} open ways at their middle vertex", file=sys.stderr)
    print(f"Handed {stats['ways_to_area_builder']} ways to the area builder", file=sys.stderr)
    print(f"Containers: {stats['containers']}", file=sys.stderr)
    print(f"Site relations: {stats['site_relations']}", file=sys.stderr)
    print(f"Water features: {stats['water']}", file=sys.stderr)
    print(f"Pubs, fuel and car washes: {stats['inside']}", file=sys.stderr)
    print(f"Major roads: {stats['major_roads']}", file=sys.stderr)
    print(f"Localities and council areas: {stats['localities']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
