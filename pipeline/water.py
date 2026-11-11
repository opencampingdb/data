"""Beaches, crocodile signs and boat ramps near each place.

The extract collects three kinds of OSM feature in the same read as the
places:

    beach       natural=beach, as an area, a node or an open line. Beaches
                are not places (see "Kind" in pipeline/README.md).
    crocodiles  a crocodile hazard: hazard=crocodile or crocodiles, or
                hazard:animal=crocodile or crocodiles, alone or in a
                semicolon list. OSM's hazard key is for hazards a sign or a
                government notice declares.
    boat_ramp   leisure=slipway, as a node, an area or an open line. Boat
                ramps are places too, of kind boat_ramp.

For every place, set_facts records:

    nearest_beach      {"distance_m": ..., "name": ...} for the closest beach
                       within NEAREST_RADIUS_M
    crocodiles         true when a crocodile hazard lies within
                       CROCODILE_RADIUS_M and the place has no crocodiles
                       value of its own
    nearest_boat_ramp  {"distance_m": ..., "name": ...} for the closest boat
                       ramp within NEAREST_RADIUS_M, other than the place
                       itself

distance_m is from the place's location to the nearest point of the
feature (0 inside an area), rounded to 10 m, so a small redraw of a beach
does not change the record. A feature never counts for the place that is
the same OSM object, so a boat ramp's nearest_boat_ramp is another ramp.
name is left out when the feature has none.
Swim spots (leisure=bathing_place and swimming_area) are places of their
own, of kind swimming_spot, so they need no fact. OSM has no tag that says
a place has no crocodiles, so the extract never sets crocodiles to false.
"""

import math

import shapely
from shapely import LineString, Point, STRtree

# Metres in one degree of latitude (and of longitude at the equator), on
# the sphere of mean radius 6,371,008.8 m.
METRES_PER_DEGREE = 111_195.0
NEAREST_RADIUS_M = 5000
CROCODILE_RADIUS_M = 1000
NAME_MAX = 100

# Pairs for osmium.filter.TagFilter, which matches whole values. Crocodile
# hazards inside a semicolon list are found only when the object also has
# one of these pairs, or is a place.
WATER_TAG_PAIRS = (
    ("natural", "beach"),
    ("hazard", "crocodile"),
    ("hazard", "crocodiles"),
    ("hazard:animal", "crocodile"),
    ("hazard:animal", "crocodiles"),
    ("leisure", "slipway"),
)

CROCODILE_VALUES = {"crocodile", "crocodiles"}


def tag_values(tags: dict, key: str) -> set[str]:
    """The values of a tag split on semicolons, lower case."""
    return {part.strip().lower() for part in tags.get(key, "").split(";") if part.strip()}


def crocodile_hazard(tags: dict) -> bool:
    """True when the tags say there is a crocodile hazard."""
    return bool((tag_values(tags, "hazard") | tag_values(tags, "hazard:animal")) & CROCODILE_VALUES)


def water_kinds(tags: dict) -> set[str]:
    """The kinds of water feature an object is: beach, crocodiles and boat_ramp."""
    kinds = set()
    if tags.get("natural") == "beach":
        kinds.add("beach")
    if tags.get("leisure") == "slipway":
        kinds.add("boat_ramp")
    if crocodile_hazard(tags):
        kinds.add("crocodiles")
    return kinds


def open_line(way):
    """A shapely line for an open way, or None when a node location is missing."""
    points = []
    for node in way.nodes:
        if not node.location.valid():
            return None
        points.append((node.location.lon, node.location.lat))
    return LineString(points) if len(points) >= 2 else None


def distance_m(lat: float, lon: float, geometry) -> int:
    """Metres from (lat, lon) to the nearest point of a geometry in lon, lat degrees.

    The geometry is projected flat around the point, with longitude scaled by
    the cosine of the latitude. Within a few kilometres that is well within a
    metre of the distance on the sphere.
    """
    scale = math.cos(math.radians(lat))

    def project(coords):
        out = coords.copy()
        out[:, 0] = (coords[:, 0] - lon) * scale * METRES_PER_DEGREE
        out[:, 1] = (coords[:, 1] - lat) * METRES_PER_DEGREE
        return out

    return round(shapely.distance(Point(0, 0), shapely.transform(geometry, project)))


class WaterFeatures:
    """The water features found in a read, and the nearest of each kind to a place."""

    def __init__(self) -> None:
        self.features: dict[str, list[tuple]] = {"beach": [], "crocodiles": [], "boat_ramp": []}
        self.trees: dict[str, STRtree] = {}

    def add(self, kinds: set[str], geometry, name: str | None, key: tuple[str, int] | None = None) -> None:
        """Add a feature. key is its (osm type, osm id), so that it never counts for itself."""
        name = (name or "").strip() or None
        if name is not None and len(name) > NAME_MAX:
            name = None
        for kind in kinds:
            self.features[kind].append((geometry, name, key))
        self.trees.clear()

    def count(self, kind: str) -> int:
        return len(self.features[kind])

    def nearest(
        self, kind: str, lat: float, lon: float, radius_m: float, exclude: tuple[str, int] | None = None
    ) -> tuple[int, str | None] | None:
        """(distance in metres, name) of the nearest feature of a kind within radius_m, or None.

        A feature whose key is exclude is passed over. Ties go to the feature
        added first, so the same file always gives the same answer.
        """
        features = self.features[kind]
        if not features:
            return None
        if kind not in self.trees:
            self.trees[kind] = STRtree([geometry for geometry, _, _ in features])
        dlat = radius_m / METRES_PER_DEGREE
        dlon = radius_m / (METRES_PER_DEGREE * max(math.cos(math.radians(lat)), 0.01))
        box = shapely.box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        best = None
        for index in sorted(self.trees[kind].query(box)):
            geometry, name, key = features[index]
            if exclude is not None and key == exclude:
                continue
            distance = distance_m(lat, lon, geometry)
            if distance <= radius_m and (best is None or distance < best[0]):
                best = (distance, name)
        return best

    def set_facts(self, record: dict, key: tuple[str, int] | None = None) -> None:
        """Set nearest_beach, crocodiles and nearest_boat_ramp on a place record whose OSM object is key."""
        lat, lon = record["location"]["lat"], record["location"]["lon"]
        facts = record["facts"]
        for kind, fact in (("beach", "nearest_beach"), ("boat_ramp", "nearest_boat_ramp")):
            found = self.nearest(kind, lat, lon, NEAREST_RADIUS_M, exclude=key)
            if found is not None:
                distance, name = found
                value = {"distance_m": (distance + 5) // 10 * 10}
                if name is not None:
                    value["name"] = name
                facts[fact] = value
        if "crocodiles" not in facts and self.nearest("crocodiles", lat, lon, CROCODILE_RADIUS_M) is not None:
            facts["crocodiles"] = True
