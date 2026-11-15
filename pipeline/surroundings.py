"""The protected area a place lies in, and the waterway and feature nearest to it.

Apps use these facts to describe a place that has no name of its own, such
as "Campground by Black Bobs Creek". Open Camping DB never makes up a name:
each fact is a named OSM object, with its name as tagged.

The extract collects three kinds of named OSM object in the same read as
the places:

    protected area  an area (a closed way or a multipolygon) tagged
                    boundary=protected_area, boundary=national_park or
                    leisure=nature_reserve, or landuse=forest with
                    "State Forest" in its name
    waterway        a way tagged waterway=river, stream, creek, canal or brook
    feature         a node or an area tagged natural=water, beach, bay, peak,
                    waterfall, spring, gorge, cave_entrance, cape or wetland,
                    or a node tagged place=locality or hamlet

Only objects with a name of at most NAME_MAX characters count. For a place,
set_facts records:

    protected_area   {"name", "osm_id", "osm_type"} for the smallest
                     protected area the place's location lies in or on the
                     edge of
    nearest_waterway {"name", "osm_id", "distance_m"} for the nearest
                     waterway within WATERWAY_RADIUS_M
    nearest_feature  {"name", "type", "osm_id", "osm_type", "distance_m"} for
                     the nearest natural feature within FEATURE_RADIUS_M, or
                     when there is none, the nearest place=locality or hamlet
                     node within PLACE_NODE_RADIUS_M. type is the natural or
                     place value.

distance_m is from the place's location to the nearest point of the
object (0 inside an area), rounded to 10 m (half up), worked out as in pipeline.water. Ties go
to the lower OSM id, so the same file always gives the same answer.
"""

import math

import shapely
from shapely import STRtree

from .water import METRES_PER_DEGREE, distance_m

NAME_MAX = 100
WATERWAY_RADIUS_M = 200
WATERWAY_TYPES = ("river", "stream", "creek", "canal", "brook")
FEATURE_RADIUS_M = 300
FEATURE_TYPES = (
    "water", "beach", "bay", "peak", "waterfall", "spring", "gorge", "cave_entrance", "cape", "wetland",
)
PLACE_NODE_RADIUS_M = 1000
PLACE_NODE_TYPES = ("locality", "hamlet")

# Pairs for osmium.filter.TagFilter. landuse=forest is a container pair
# already (pipeline.kinds).
SURROUNDINGS_TAG_PAIRS = (
    ("boundary", "protected_area"),
    ("boundary", "national_park"),
    ("leisure", "nature_reserve"),
) + tuple(("waterway", value) for value in WATERWAY_TYPES) + tuple(
    ("natural", value) for value in FEATURE_TYPES
) + tuple(("place", value) for value in PLACE_NODE_TYPES)


def clean_name(tags: dict) -> str | None:
    """The object's name, or None when it has none or it is longer than NAME_MAX."""
    name = (tags.get("name") or "").strip()
    return name if name and len(name) <= NAME_MAX else None


def is_protected_area(tags: dict) -> bool:
    """True for a named protected area, national park, nature reserve or state forest."""
    name = clean_name(tags)
    if name is None:
        return False
    if tags.get("boundary") in ("protected_area", "national_park") or tags.get("leisure") == "nature_reserve":
        return True
    return tags.get("landuse") == "forest" and "state forest" in name.lower()


def is_waterway(tags: dict) -> bool:
    """True for a named river, stream, creek, canal or brook."""
    return tags.get("waterway") in WATERWAY_TYPES and clean_name(tags) is not None


def feature_type(tags: dict) -> str | None:
    """The natural value of a named natural feature, else None."""
    natural = tags.get("natural")
    return natural if natural in FEATURE_TYPES and clean_name(tags) is not None else None


def place_node_type(tags: dict) -> str | None:
    """locality or hamlet for a named place=locality or hamlet, else None."""
    place = tags.get("place")
    return place if place in PLACE_NODE_TYPES and clean_name(tags) is not None else None


def round_10(metres: int) -> int:
    """Whole metres rounded to 10, half up."""
    return (metres + 5) // 10 * 10


def search_box(lat: float, lon: float, radius_m: float):
    """A box in lon, lat degrees that holds every point within radius_m of (lat, lon)."""
    dlat = radius_m / METRES_PER_DEGREE
    dlon = radius_m / (METRES_PER_DEGREE * max(math.cos(math.radians(lat)), 0.01))
    return shapely.box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)


class Nearest:
    """Named objects of one sort, and the nearest of them to a point."""

    def __init__(self) -> None:
        # (osm type, osm id, name, extra, geometry)
        self.items: list[tuple] = []
        self.tree: STRtree | None = None

    def add(self, key: tuple[str, int], name: str, extra, geometry) -> None:
        self.items.append((key[0], key[1], name, extra, geometry))
        self.tree = None

    def __len__(self) -> int:
        return len(self.items)

    def nearest(self, lat: float, lon: float, radius_m: float, exclude: tuple[str, int] | None = None):
        """(item, metres) for the nearest item within radius_m other than exclude, or None.

        Ties go to the lower osm id, then the lower osm type.
        """
        if not self.items:
            return None
        if self.tree is None:
            self.tree = STRtree([item[4] for item in self.items])
        best = None
        for index in self.tree.query(search_box(lat, lon, radius_m)).tolist():
            item = self.items[index]
            if exclude is not None and item[:2] == exclude:
                continue
            metres = distance_m(lat, lon, item[4])
            if metres > radius_m:
                continue
            rank = (metres, item[1], item[0])
            if best is None or rank < best[0]:
                best = (rank, item)
        return None if best is None else (best[1], best[0][0])


class Surroundings:
    """The named protected areas, waterways and features found in a read."""

    def __init__(self) -> None:
        # (area in square degrees, osm type, osm id, name, geometry)
        self.areas: list[tuple] = []
        self.area_tree: STRtree | None = None
        self.waterways = Nearest()
        self.features = Nearest()
        self.place_nodes = Nearest()

    def add_protected_area(self, key: tuple[str, int], tags: dict, geometry) -> None:
        if geometry is None or geometry.is_empty or not is_protected_area(tags):
            return
        self.areas.append((geometry.area, key[0], key[1], clean_name(tags), geometry))
        self.area_tree = None

    def add_waterway(self, key: tuple[str, int], tags: dict, line) -> None:
        if line is None or line.is_empty or not is_waterway(tags):
            return
        self.waterways.add(key, clean_name(tags), None, line)

    def add_feature(self, key: tuple[str, int], tags: dict, geometry) -> None:
        """Add a natural feature: a point for a node, a polygon for an area."""
        kind = feature_type(tags)
        if geometry is None or geometry.is_empty or kind is None:
            return
        self.features.add(key, clean_name(tags), kind, geometry)

    def add_place_node(self, osm_id: int, tags: dict, lat: float, lon: float) -> None:
        kind = place_node_type(tags)
        if kind is None:
            return
        self.place_nodes.add(("node", osm_id), clean_name(tags), kind, shapely.Point(lon, lat))

    def counts(self) -> dict:
        return {
            "protected_areas": len(self.areas),
            "waterways": len(self.waterways),
            "features": len(self.features),
            "localities_and_hamlets": len(self.place_nodes),
        }

    def protected_areas_of(self, points: list[tuple[float, float]]) -> list[dict | None]:
        """For each (lat, lon) point, the smallest protected area it lies in, or None.

        Ties of area go to the lower (osm type, osm id), so the same file
        always gives the same answer.
        """
        found: list[dict | None] = [None] * len(points)
        if not points or not self.areas:
            return found
        if self.area_tree is None:
            self.area_tree = STRtree([area[4] for area in self.areas])
        query = shapely.points([(lon, lat) for lat, lon in points])
        point_index, area_index = self.area_tree.query(query, predicate="intersects")
        best: dict[int, int] = {}
        for p, a in zip(point_index.tolist(), area_index.tolist()):
            if p not in best or self.areas[a][:3] < self.areas[best[p]][:3]:
                best[p] = a
        for p, a in best.items():
            _, osm_type, osm_id, name, _ = self.areas[a]
            found[p] = {"name": name, "osm_id": osm_id, "osm_type": osm_type}
        return found

    def set_facts(self, records: list[dict]) -> None:
        """Set protected_area, nearest_waterway and nearest_feature on every place record."""
        areas = self.protected_areas_of([(r["location"]["lat"], r["location"]["lon"]) for r in records])
        for record, area in zip(records, areas):
            facts = record["facts"]
            lat, lon = record["location"]["lat"], record["location"]["lon"]
            key = (record["osm"]["type"], record["osm"]["id"])
            if area is not None:
                facts["protected_area"] = area
            found = self.waterways.nearest(lat, lon, WATERWAY_RADIUS_M, exclude=key)
            if found is not None:
                (_, osm_id, name, _, _), metres = found
                facts["nearest_waterway"] = {"name": name, "osm_id": osm_id, "distance_m": round_10(metres)}
            found = self.features.nearest(lat, lon, FEATURE_RADIUS_M, exclude=key) or self.place_nodes.nearest(
                lat, lon, PLACE_NODE_RADIUS_M, exclude=key
            )
            if found is not None:
                (osm_type, osm_id, name, kind, _), metres = found
                facts["nearest_feature"] = {
                    "name": name,
                    "type": kind,
                    "osm_id": osm_id,
                    "osm_type": osm_type,
                    "distance_m": round_10(metres),
                }
