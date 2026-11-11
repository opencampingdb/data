"""A pub, a fuel station or a car wash inside a place.

The extract collects three kinds of OSM object in the same read as the
places, each as one point: a node at its position, a closed way or a
multipolygon at the centroid of its area.

    amenity=pub       sets pub
    amenity=fuel      sets fuel
    amenity=car_wash  sets car_wash

set_facts sets each fact to true on a place that is an area (a closed way or
a multipolygon) with such a point inside it or on its edge. An object never
counts for itself, so a fuel station drawn as an area does not get fuel from
its own centroid. A place drawn as a node has no area and gets none of these.
A place whose own tags set the fact keeps its own value. OSM has no tag that
says a place has no pub, so the extract never sets these facts to false.
"""

import shapely
from shapely import STRtree

# amenity value -> fact key.
INSIDE_AMENITIES = {"pub": "pub", "fuel": "fuel", "car_wash": "car_wash"}

# Pairs for osmium.filter.TagFilter. pub and fuel are places, so they pass
# the place filter too.
NEARBY_TAG_PAIRS = tuple(("amenity", value) for value in INSIDE_AMENITIES)


def inside_fact(tags: dict) -> str | None:
    """The fact an object sets on a place it lies inside, or None."""
    return INSIDE_AMENITIES.get(tags.get("amenity", ""))


class InsideFeatures:
    """The pubs, fuel stations and car washes found in a read, as points."""

    def __init__(self) -> None:
        self.points: list[tuple[float, float]] = []  # (lon, lat)
        self.facts: list[str] = []
        self.keys: list[tuple[str, int]] = []
        self.tree: STRtree | None = None

    def add(self, fact: str, lat: float, lon: float, key: tuple[str, int]) -> None:
        self.points.append((lon, lat))
        self.facts.append(fact)
        self.keys.append(key)
        self.tree = None

    def count(self, fact: str) -> int:
        return self.facts.count(fact)

    def set_facts(self, record: dict, outline, key: tuple[str, int]) -> None:
        """Set pub, fuel and car_wash on a place record whose area is outline (None for a node)."""
        if outline is None or not self.points:
            return
        if self.tree is None:
            self.tree = STRtree(shapely.points(self.points))
        facts = record["facts"]
        for index in self.tree.query(outline, predicate="intersects"):
            fact = self.facts[index]
            if self.keys[index] != key and fact not in facts:
                facts[fact] = True
