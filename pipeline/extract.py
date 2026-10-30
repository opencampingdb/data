"""Read an OSM file and write the camping places in it as NDJSON.

Usage:
    python -m pipeline.extract australia-latest.osm.pbf --out extract.ndjson

Reads .osm.pbf or .osm XML with pyosmium. Keeps nodes, closed ways and
multipolygon relations that carry one of the SPEC tags. A node is placed at
its own position. A closed way or a multipolygon is placed at the centroid of
its area (holes taken out). Other relations and open ways are skipped.

Each output line is one place:
    {"osm": {...}, "kind": "...", "name": ..., "location": {...}, "facts": {...}}
sorted by OSM type and then id, so the same input always gives the same file.
The tag to fact mapping is documented in pipeline/README.md.
"""

import argparse
import re
import sys
from pathlib import Path

import osmium

from .common import dump_line, round_coord, write_text

# (OSM key, OSM value, place kind). When an object matches more than one,
# the first match in this list wins, so a camp site that is also tagged
# amenity=toilets stays a camp site.
KIND_TAGS = (
    ("tourism", "camp_site", "camp_site"),
    ("tourism", "caravan_site", "caravan_site"),
    ("highway", "rest_area", "rest_area"),
    ("amenity", "sanitary_dump_station", "dump_station"),
    ("amenity", "drinking_water", "drinking_water"),
    ("amenity", "toilets", "toilets"),
    ("amenity", "shower", "shower"),
    ("tourism", "picnic_site", "picnic_site"),
)

# Simple yes/no tags: OSM key -> fact key.
YES_NO_TAGS = {
    "power_supply": "power",
    "tents": "tents",
    "caravans": "caravans",
    "toilets": "toilets",
    "drinking_water": "drinking_water",
    "sanitary_dump_station": "dump_station",
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


def kind_from_tags(tags: dict) -> str | None:
    """The place kind for a set of OSM tags, or None if it is not a place we keep."""
    for key, value, kind in KIND_TAGS:
        if tags.get(key) == value:
            return kind
    return None


def first_value(value: str) -> str:
    """OSM allows several values separated by ';'. Keep the first."""
    return value.split(";")[0].strip()


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


def parse_fee(value: str) -> bool | None:
    """fee=no is free. fee=yes or a price (any value with a digit or $) is paid."""
    value = value.strip().lower()
    if value == "no":
        return False
    if value == "yes" or "$" in value or any(c.isdigit() for c in value):
        return True
    return None


def parse_maxstay(value: str) -> str:
    """Shorten common maxstay forms ('48 hours' -> '48h'). Keep anything else as written."""
    value = value.strip()
    match = MAXSTAY_RE.match(value.lower())
    if match and match.group(2) in MAXSTAY_UNITS:
        return match.group(1) + MAXSTAY_UNITS[match.group(2)]
    return value


def facts_from_tags(tags: dict) -> dict:
    """Map OSM tags to the facts in a place record. Unknown values are left out."""
    facts = {}

    if "fee" in tags:
        fee = parse_fee(tags["fee"])
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

    dog = tags.get("dog", "").strip().lower()
    if dog in ("yes", "leashed", "no"):
        facts["pets"] = dog

    if tags.get("maxstay", "").strip():
        facts["max_stay"] = parse_maxstay(tags["maxstay"])

    if tags.get("opening_hours", "").strip():
        facts["opening_hours"] = tags["opening_hours"].strip()

    # website wins over contact:website. Only http and https links are kept.
    for key in ("website", "contact:website"):
        if key in tags:
            url = first_value(tags[key])
            if url.lower().startswith(("http://", "https://")):
                facts["website"] = url
            break

    for key in ("phone", "contact:phone"):
        if key in tags and first_value(tags[key]):
            facts["phone"] = first_value(tags[key])
            break

    four_wd = tags.get("4wd_only", "").strip().lower()
    if four_wd == "yes":
        facts["access"] = "4wd"
    elif four_wd == "recommended":
        facts["access"] = "awd"
    elif four_wd == "no":
        facts["access"] = "2wd"

    # access=* says who may enter. It maps to "entry", not to "access", which
    # is about the road.
    entry = ENTRY_VALUES.get(first_value(tags.get("access", "")).lower())
    if entry:
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


def place_tag_filter() -> "osmium.filter.TagFilter":
    """A C++ filter that lets through only objects with one of the SPEC tags.

    A filter object belongs to one handler chain, so each chain gets its own.
    """
    return osmium.filter.TagFilter(*((k, v) for k, v, _ in KIND_TAGS))


class AreaCandidates:
    """First pass: finds the ways the main pass must keep.

    Sees only ways and relations that carry a SPEC tag. Records the closed
    tagged ways (places in their own right) and the member ways of tagged
    multipolygon relations (needed to assemble those relations' areas).
    Every other way, buildings included, is kept away from the area builder.
    """

    def __init__(self, stats: dict) -> None:
        self.way_ids: set[int] = set()
        self.stats = stats

    def way(self, way) -> None:
        if way.is_closed():
            self.way_ids.add(way.id)
        else:
            self.stats["open_ways"] += 1

    def relation(self, relation) -> None:
        if relation.tags.get("type") == "multipolygon":
            for member in relation.members:
                if member.type == "w":
                    self.way_ids.add(member.ref)


class AreaWayCounter:
    """Counts the ways handed to the area builder. Passes every object on."""

    def __init__(self, stats: dict) -> None:
        self.stats = stats

    def way(self, way) -> None:
        self.stats["ways_to_area_builder"] += 1


def read_places(path: Path, stats: dict):
    """Yield every OSM object or area with a SPEC tag, with node locations filled in.

    Two passes over the file:
    1. Ways and relations only, no locations: collect the ways that matter
       (AreaCandidates), and let the area builder choose its tagged
       multipolygon relations.
    2. Everything, with a node location cache. A way id filter sits in front
       of the area builder, so it never sees a way it does not need. Only
       objects with a SPEC tag come out at the end.

    pyosmium's FileProcessor.with_areas() filters only the relations, and
    would assemble an area for every closed way in the file, so this chain
    is built by hand.
    """
    # Every native object is held in a local variable for the whole read:
    # the handlers keep only pointers to each other, so an object that
    # Python frees early crashes the reader.
    area_manager = osmium.area.AreaManager()
    candidates = AreaCandidates(stats)
    first_filter = place_tag_filter()
    first_pass = area_manager.first_pass_handler()
    with osmium.io.Reader(str(path), osmium.osm.WAY | osmium.osm.RELATION) as reader:
        osmium.apply(reader, first_filter, first_pass, candidates)

    area_gate = osmium.filter.IdFilter(candidates.way_ids)
    area_gate.enable_for(osmium.osm.WAY)
    area_filter = place_tag_filter()
    areas = osmium.BufferIterator(area_filter)
    second_pass = area_manager.second_pass_to_buffer(areas)
    counter = AreaWayCounter(stats)
    output_filter = place_tag_filter()
    location_store = osmium.index.create_map("flex_mem")
    locations = osmium.NodeLocationsForWays(location_store)
    locations.ignore_errors()

    with osmium.io.Reader(str(path)) as reader:
        objects = osmium.OsmFileIterator(
            reader, locations, area_gate, counter, second_pass, output_filter
        )
        for obj in objects:
            yield obj
            if areas:
                yield from areas
    if areas:
        yield from areas


def place_record(obj, stats: dict) -> dict | None:
    """The extract record for one OSM object or area, or None if it is not a place."""
    tags = {tag.k: tag.v for tag in obj.tags}
    kind = kind_from_tags(tags)
    if kind is None:
        return None

    if obj.is_node():
        osm_type, osm_id = "node", obj.id
        if not obj.location.valid():
            stats["missing_locations"] += 1
            return None
        lat, lon = obj.location.lat, obj.location.lon
    elif obj.is_relation():
        # A relation is used through the area built from it.
        return None
    elif obj.is_area():
        # Closed ways are handled as ways, so their areas are not used.
        # Only multipolygon relations produce areas here. Any other relation
        # (type=site, for example) has no area and is skipped.
        if obj.from_way():
            return None
        osm_type, osm_id = "relation", obj.orig_id()
        centroid = relation_area_centroid(obj)
        if centroid is None:
            stats["missing_locations"] += 1
            return None
        lat, lon = centroid
    else:
        osm_type, osm_id = "way", obj.id
        if not obj.is_closed():
            return None  # already counted in the first pass
        centroid = way_centroid(obj)
        if centroid is None:
            stats["missing_locations"] += 1
            return None
        lat, lon = centroid

    return {
        "osm": {"type": osm_type, "id": osm_id, "version": obj.version},
        "kind": kind,
        "name": tags.get("name", "").strip() or None,
        "location": {"lat": round_coord(lat), "lon": round_coord(lon)},
        "facts": facts_from_tags(tags),
    }


def extract_file(path: Path, stats: dict | None = None) -> list[dict]:
    """Read an OSM file and return the place records found in it, sorted.

    If a dict is passed as stats it is filled with counts of what was kept
    and skipped, and of the ways handed to the area builder, for the log.
    """
    stats = stats if stats is not None else {}
    stats.update(kept=0, open_ways=0, missing_locations=0, ways_to_area_builder=0)
    records = []
    for obj in read_places(path, stats):
        record = place_record(obj, stats)
        if record is not None:
            records.append(record)
            stats["kept"] += 1
    records.sort(key=lambda r: (r["osm"]["type"], r["osm"]["id"]))
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help=".osm.pbf or .osm file")
    parser.add_argument("--out", type=Path, default=Path("extract.ndjson"))
    args = parser.parse_args(argv)

    stats: dict = {}
    records = extract_file(args.input, stats)
    write_text(args.out, "".join(dump_line(r) + "\n" for r in records))

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
    print(f"Handed {stats['ways_to_area_builder']} ways to the area builder", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
