"""Which Australian state or territory a point is in, from Natural Earth's
public domain state boundaries.

boundaries/au-states.geojson holds the Australian features of Natural Earth's
1:10m Admin 1 (states and provinces) layer, each with a "state" code:

    NSW, VIC, QLD, SA, WA, TAS, NT, ACT
        The six states and two mainland territories. Lord Howe Island counts
        with NSW and Macquarie Island with TAS, which govern them.
    OT
        The Other Territories: Jervis Bay Territory, Norfolk Island, Christmas
        Island and the Cocos (Keeling) Islands, as the Australian Bureau of
        Statistics groups them.

Natural Earth's coastline is generalised, so a place on a beach, a jetty or a
small island can fall just outside it. A point outside every outline takes
the nearest state within COAST_M, three nautical miles, the breadth of the
coastal waters that belong to the adjacent state (Coastal Waters (State
Powers) Act 1980). A point further out is in no state, and state_of returns
None.

Natural Earth: "All versions of Natural Earth raster + vector map data found
on this website are in the public domain."
https://www.naturalearthdata.com/about/terms-of-use/

To rebuild the file from a newer Natural Earth version:

    python -m pipeline.states --extract ne_10m_admin_1_states_provinces.geojson \\
        --version 5.1.2 --out boundaries/au-states.geojson
"""

import argparse
import json
import math
import sys
from pathlib import Path

from shapely import prepare
from shapely.geometry import Point, box, shape
from shapely.ops import nearest_points
from shapely.strtree import STRtree

BOUNDARIES = Path(__file__).resolve().parent.parent / "boundaries" / "au-states.geojson"
# Three nautical miles.
COAST_M = 5556.0
EARTH_RADIUS_M = 6371008.8

# Natural Earth's iso_3166_2 (or, where it has none of its own, its name) to a state code.
NATURAL_EARTH_STATES = {
    "AU-NSW": "NSW",
    "AU-VIC": "VIC",
    "AU-QLD": "QLD",
    "AU-SA": "SA",
    "AU-WA": "WA",
    "AU-TAS": "TAS",
    "AU-NT": "NT",
    "AU-ACT": "ACT",
    "Macquarie Island": "TAS",
    "Jervis Bay Territory": "OT",
    "Norfolk Island": "OT",
    "Christmas Island": "OT",
    "Cocos (Keeling) Islands": "OT",
}


def state_code(properties: dict) -> str | None:
    """The state code of one Natural Earth Admin 1 feature, or None for one outside Australia's states and territories."""
    return NATURAL_EARTH_STATES.get(properties.get("iso_3166_2")) or NATURAL_EARTH_STATES.get(properties.get("name"))


def extract(natural_earth: dict, version: str) -> dict:
    """The Australian features of a Natural Earth Admin 1 GeoJSON, as written to boundaries/au-states.geojson."""
    features = []
    for feature in natural_earth["features"]:
        code = state_code(feature["properties"])
        if code:
            features.append({
                "type": "Feature",
                "properties": {"state": code, "name": feature["properties"]["name"]},
                "geometry": feature["geometry"],
            })
    features.sort(key=lambda f: (f["properties"]["state"], f["properties"]["name"]))
    return {
        "type": "FeatureCollection",
        "source": (
            f"Natural Earth {version}, 1:10m Admin 1 states and provinces, public domain. "
            "https://www.naturalearthdata.com/"
        ),
        "features": features,
    }


def metres_between(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


class StateLookup:
    """state_of(lon, lat) over the outlines in a boundaries file."""

    def __init__(self, path: Path = BOUNDARIES):
        with open(path, encoding="utf-8") as f:
            collection = json.load(f)
        self.codes = [feature["properties"]["state"] for feature in collection["features"]]
        self.shapes = [shape(feature["geometry"]) for feature in collection["features"]]
        for geometry in self.shapes:
            prepare(geometry)
        self.tree = STRtree(self.shapes)

    def state_of(self, lon: float, lat: float) -> str | None:
        """The state code the point is in, the nearest within COAST_M of a coast, or None."""
        point = Point(lon, lat)
        inside = sorted(int(i) for i in self.tree.query(point, predicate="intersects"))
        if inside:
            return self.codes[inside[0]]
        dlat = math.degrees(COAST_M / EARTH_RADIUS_M)
        dlon = dlat / max(math.cos(math.radians(lat)), 0.01)
        bounds = (lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        best, best_m = None, COAST_M
        for i in sorted(int(i) for i in self.tree.query(box(*bounds))):
            nearest = nearest_points(self.shapes[i], point)[0]
            metres = metres_between(lon, lat, nearest.x, nearest.y)
            if metres <= best_m:
                best, best_m = self.codes[i], metres
        return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--extract", type=Path, required=True, help="Natural Earth ne_10m_admin_1_states_provinces.geojson")
    parser.add_argument("--version", required=True, help="the Natural Earth version, for example 5.1.2")
    parser.add_argument("--out", type=Path, default=BOUNDARIES)
    args = parser.parse_args(argv)
    with open(args.extract, encoding="utf-8") as f:
        collection = extract(json.load(f), args.version)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(collection, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"{len(collection['features'])} features written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
