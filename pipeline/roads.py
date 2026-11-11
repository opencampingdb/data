"""The nearest major road to each place.

The extract reads every major road in the same pass as the places: a way
whose highway value pipeline.geometry.is_major_road accepts (motorway,
trunk, primary and secondary, their _link roads, and a one-way tertiary
road, which is one carriageway of a divided road). Each road is a line
through its nodes, closed or not, so a roundabout counts by its kerb line.
The geometry sidecar's road lines come from the same read.

For every place, set_facts records:

    nearest_major_road  {"distance_m": ..., "name": ...} for the closest
                        major road within NEAREST_ROAD_RADIUS_M

distance_m is from the place's location to the nearest point of the road,
on the same flat projection as pipeline.water and rounded to 10 m (half up),
so a small redraw of a road does not change the record. name is the road's
name, or its ref when it has no name, and is left out when it has neither
(or when it is longer than 100 characters). Ties go to the road read first.
"""

import math

import numpy as np
import shapely
from shapely import STRtree

from .geometry import is_major_road
from .water import METRES_PER_DEGREE, NAME_MAX, distance_m

# Past 500 m a major road says little about the place, and the work grows
# with the radius: on the Australian extract, 5 km took the read from about
# 1.5 minutes to 9.5, for 19% more places with a value.
NEAREST_ROAD_RADIUS_M = 500


class MajorRoads:
    """The major roads found in a read, and the nearest one to a place.

    roads holds (way id, highway, name, ref, [(lon, lat), ...]) for each
    road, in the order read, the form pipeline.geometry.SidecarBuilder
    writes its road lines from.
    """

    def __init__(self) -> None:
        self.roads: list[tuple] = []
        self.tree: STRtree | None = None
        self.lines: list = []

    def way(self, way) -> None:
        """A way handler: keeps major roads, with their node locations."""
        tags = way.tags
        if not is_major_road(tags):
            return
        coords = []
        for node in way.nodes:
            if not node.location.valid():
                return
            coords.append((round(node.location.lon, 7), round(node.location.lat, 7)))
        if len(coords) >= 2:
            self.roads.append((way.id, tags.get("highway"), tags.get("name"), tags.get("ref"), coords))
            self.tree = None

    def count(self) -> int:
        return len(self.roads)

    def nearest(self, lat: float, lon: float, radius_m: float) -> tuple[int, str | None] | None:
        """(distance in metres, name or ref) of the nearest major road within radius_m, or None."""
        if not self.roads:
            return None
        if self.tree is None:
            counts = [len(coords) for *_, coords in self.roads]
            flat = np.array([point for *_, coords in self.roads for point in coords], dtype=float)
            self.lines = shapely.linestrings(flat, indices=np.repeat(np.arange(len(counts)), counts))
            self.tree = STRtree(self.lines)
        dlat = radius_m / METRES_PER_DEGREE
        dlon = radius_m / (METRES_PER_DEGREE * max(math.cos(math.radians(lat)), 0.01))
        box = shapely.box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        best = None
        for index in sorted(self.tree.query(box)):
            distance = distance_m(lat, lon, self.lines[index])
            if distance <= radius_m and (best is None or distance < best[0]):
                _, _, name, ref, _ = self.roads[index]
                best = (distance, road_label(name, ref))
        return best

    def set_facts(self, record: dict) -> None:
        """Set nearest_major_road on a place record."""
        found = self.nearest(record["location"]["lat"], record["location"]["lon"], NEAREST_ROAD_RADIUS_M)
        if found is None:
            return
        distance, name = found
        value = {"distance_m": (distance + 5) // 10 * 10}
        if name is not None:
            value["name"] = name
        record["facts"]["nearest_major_road"] = value


def road_label(name: str | None, ref: str | None) -> str | None:
    """The road's name, else its ref, else None. A label over NAME_MAX characters is None."""
    for label in (name, ref):
        label = (label or "").strip()
        if label:
            return label if len(label) <= NAME_MAX else None
    return None
