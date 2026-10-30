"""Read an OSM file and write the places in it as NDJSON.

Usage:
    python -m pipeline.extract australia-latest.osm.pbf --out extract.ndjson

Reads .osm.pbf or .osm XML with pyosmium. Keeps nodes, closed ways and
multipolygon relations that pipeline.kinds classifies as a place. A node is
placed at its own position. A closed way or a multipolygon is placed at the
centroid of its area (holes taken out). An open way that is a boat ramp or
an attraction (slipways and walls are often drawn as lines) is placed at its
middle vertex. Other relations and open ways are skipped.

Camp sites are classified by the areas they lie in: national parks and other
parks, state forests and showgrounds. Those areas are built in the same read
and tested with an STRtree (shapely).

Each output line is one place:
    {"osm": {...}, "kind": "...", "name": ..., "location": {...}, "facts": {...}}
sorted by OSM type and then id, so the same input always gives the same file.
The tag mappings are documented in pipeline/README.md.
"""

import argparse
import re
import sys
from pathlib import Path

import osmium
import shapely
from shapely import MultiPolygon, Polygon, STRtree

from .common import dump_line, round_coord, write_text
from .kinds import (
    CONTAINER_TAG_PAIRS,
    PLACE_TAG_PAIRS,
    classify,
    container_type,
    parse_fee,
    water_type,
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


# Kinds that are often mapped as an open line, such as a slipway drawn down
# into the water. Such a way is placed at its middle vertex. Open ways of
# every other kind are skipped.
OPEN_WAY_KINDS = ("boat_ramp", "attraction")


def interesting_tag_filter() -> "osmium.filter.TagFilter":
    """A C++ filter that lets through objects that may be a place or a container.

    A filter object belongs to one handler chain, so each chain gets its own.
    """
    return osmium.filter.TagFilter(*PLACE_TAG_PAIRS, *CONTAINER_TAG_PAIRS)


def tag_dict(obj) -> dict:
    return {tag.k: tag.v for tag in obj.tags}


class FirstPass:
    """First pass: finds the ways the main pass must keep.

    Sees only ways and relations that passed the tag filter. Records the
    closed ways that are a place or a container, and the member ways of
    multipolygon and boundary relations that are a place or a container.
    Every other way, buildings included, is kept away from the area builder.

    relation() returns True for a relation the area builder must not
    assemble, which drops it from the rest of the handler chain.
    """

    def __init__(self, stats: dict) -> None:
        self.way_ids: set[int] = set()
        self.stats = stats

    def way(self, way) -> None:
        tags = tag_dict(way)
        kind = classify(tags)
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
        if classify(tags) is None and container_type(tags) is None:
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


def read_objects(path: Path, stats: dict):
    """Yield every OSM object or area that may be a place or a container.

    Two passes over the file:
    1. Ways and relations only, no locations: collect the ways that matter
       (FirstPass), and let the area builder take note of the relations
       FirstPass keeps.
    2. Everything, with a node location cache. A way id filter sits in front
       of the area builder, so it never sees a way it does not need. Only
       objects with an interesting tag come out at the end.

    pyosmium's FileProcessor.with_areas() filters only the relations, and
    would assemble an area for every closed way in the file, so this chain
    is built by hand.
    """
    # Every native object is held in a local variable for the whole read:
    # the handlers keep only pointers to each other, so an object that
    # Python frees early crashes the reader.
    area_manager = osmium.area.AreaManager()
    first = FirstPass(stats)
    first_filter = interesting_tag_filter()
    first_pass = area_manager.first_pass_handler()
    with osmium.io.Reader(str(path), osmium.osm.WAY | osmium.osm.RELATION) as reader:
        osmium.apply(reader, first_filter, first, first_pass)

    area_gate = osmium.filter.IdFilter(first.way_ids)
    area_gate.enable_for(osmium.osm.WAY)
    area_filter = interesting_tag_filter()
    areas = osmium.BufferIterator(area_filter)
    second_pass = area_manager.second_pass_to_buffer(areas)
    counter = AreaWayCounter(stats)
    output_filter = interesting_tag_filter()
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


def extract_file(path: Path, stats: dict | None = None) -> list[dict]:
    """Read an OSM file and return the place records found in it, sorted.

    If a dict is passed as stats it is filled with counts of what was kept
    and skipped, of the ways handed to the area builder and of the
    containers found, for the log.
    """
    stats = stats if stats is not None else {}
    stats.update(
        kept=0, open_ways=0, open_way_places=0, missing_locations=0, ways_to_area_builder=0
    )
    container_counts = {"park": 0, "forest": 0, "showground": 0}

    places: list[tuple[dict, dict]] = []  # (record without kind, tags)
    containers: list[tuple] = []  # (shapely geometry, container type)

    for obj in read_objects(path, stats):
        tags = tag_dict(obj)

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
            "osm": {"type": osm_type, "id": osm_id, "version": obj.version},
            "name": tags.get("name", "").strip() or None,
            "location": {"lat": round_coord(lat), "lon": round_coord(lon)},
            "facts": facts_from_tags(tags),
        }
        places.append((record, tags))

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
        records.append(record)

    stats["kept"] = len(records)
    stats["containers"] = container_counts
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
    print(f"Placed {stats['open_way_places']} open ways at their middle vertex", file=sys.stderr)
    print(f"Handed {stats['ways_to_area_builder']} ways to the area builder", file=sys.stderr)
    print(f"Containers: {stats['containers']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
