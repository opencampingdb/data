"""Which Australian state or territory a point is in, from OpenStreetMap's
state and territory boundaries.

boundaries/au-states.geojson holds OSM's boundary=administrative,
admin_level=4 relations inside Australia (relation 80500), simplified, each
with a "state" code and its OSM relation id. The code is the relation's ref
tag:

    NSW, VIC, QLD, SA, WA, TAS, NT, ACT, JBT (Jervis Bay Territory),
    CX (Christmas Island), CC (Cocos (Keeling) Islands),
    HIMI (Heard Island and McDonald Islands)

Three relations have no ref in OSM, and take the code in NO_REF_CODES:

    NF  Norfolk Island, from the relation's ISO3166-1 tag.
    CR  Coral Sea Islands, its FIPS 10-4 code (no ISO 3166 code exists).
    AT  Ashmore and Cartier Islands, its FIPS 10-4 code (no ISO 3166 code exists).

OSM's outlines take in the coastal waters, so places on beaches, jetties
and near-shore islands fall inside them. A point outside every outline, such
as a pontoon on the outer Great Barrier Reef, is in no state, and state_of
returns None, as OSM has it.

The outlines are not simplified: the land borders follow rivers, and
simplifying them moves places on the riverbanks into the other state.

The outlines are © OpenStreetMap contributors, under the ODbL 1.0, like the
rest of Open Camping DB.

To rebuild the file from OSM through Overpass:

    python -m pipeline.states --fetch --out boundaries/au-states.geojson

or from a saved Overpass response (JSON, "out geom"):

    python -m pipeline.states --overpass response.json --out boundaries/au-states.geojson
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import shapely
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

BOUNDARIES = Path(__file__).resolve().parent.parent / "boundaries" / "au-states.geojson"
AUSTRALIA_RELATION = 80500
OVERPASS_QUERY = (
    "[out:json][timeout:600];"
    f"rel({AUSTRALIA_RELATION});map_to_area->.au;"
    "rel(area.au)[boundary=administrative][admin_level=4];"
    "out geom;"
)
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "Open Camping DB state boundaries (https://github.com/opencampingdb/data)"
# Codes for the relations with no ref tag in OSM.
NO_REF_CODES = {
    2574988: "NF",  # Norfolk Island: its ISO3166-1 tag in OSM.
    3225677: "CR",  # Coral Sea Islands: FIPS 10-4.
    2559345: "AT",  # Ashmore and Cartier Islands: FIPS 10-4.
}
# Not simplified: the land borders follow rivers, and simplifying them moves
# riverbank places into the wrong state (see pipeline/README.md).
SIMPLIFY_DEGREES = 0.0


def fetch_overpass(urls=OVERPASS_URLS, attempts: int = 6) -> dict:
    """The Overpass response for OVERPASS_QUERY, retried with backoff while the servers are busy."""
    data = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode()
    wait = 30
    for attempt in range(attempts):
        url = urls[attempt % len(urls)]
        request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            print(f"{url}: {error}, trying again in {wait} s", file=sys.stderr)
            time.sleep(wait)
            wait *= 2
    raise RuntimeError("Overpass did not answer")


def code_of(relation: dict) -> str | None:
    return relation.get("tags", {}).get("ref") or NO_REF_CODES.get(relation["id"])


def relation_shape(relation: dict):
    """The (multi)polygon of an Overpass "out geom" relation: its outer rings less its inner ones."""
    lines = {"outer": [], "inner": []}
    for member in relation["members"]:
        if member["type"] == "way" and member.get("geometry") and member.get("role") in ("outer", "inner", ""):
            coords = [(point["lon"], point["lat"]) for point in member["geometry"]]
            if len(coords) >= 2:
                lines["inner" if member["role"] == "inner" else "outer"].append(LineString(coords))
    outer = unary_union(list(polygonize(unary_union(lines["outer"]))))
    if lines["inner"]:
        outer = outer.difference(unary_union(list(polygonize(unary_union(lines["inner"])))))
    return shapely.make_valid(outer)


def from_overpass(response: dict, tolerance: float = SIMPLIFY_DEGREES) -> dict:
    """boundaries/au-states.geojson from an Overpass response, simplified as one coverage so shared borders stay shared."""
    relations = sorted(
        (element for element in response["elements"] if element["type"] == "relation" and code_of(element)),
        key=lambda relation: (code_of(relation), relation["id"]),
    )
    shapes = [relation_shape(relation) for relation in relations]
    if tolerance:
        shapes = shapely.coverage_simplify(shapes, tolerance, simplify_boundary=True)
    features = []
    for relation, geometry in zip(relations, shapes):
        geometry = shapely.make_valid(shapely.set_precision(geometry, 1e-7))
        features.append({
            "type": "Feature",
            "properties": {
                "state": code_of(relation),
                "name": relation["tags"].get("name"),
                "osm_relation": relation["id"],
            },
            "geometry": mapping(geometry),
        })
    return {
        "type": "FeatureCollection",
        "source": (
            "OpenStreetMap boundary=administrative admin_level=4 relations in relation "
            f"{AUSTRALIA_RELATION}" + (f", simplified to {tolerance} degrees. " if tolerance else ". ")
            + "© OpenStreetMap contributors, ODbL 1.0."
        ),
        "osm_base": response.get("osm3s", {}).get("timestamp_osm_base"),
        "features": features,
    }


class StateLookup:
    """state_of(lon, lat) over the outlines in a boundaries file."""

    def __init__(self, path: Path = BOUNDARIES):
        with open(path, encoding="utf-8") as f:
            collection = json.load(f)
        self.codes = [feature["properties"]["state"] for feature in collection["features"]]
        self.shapes = [shape(feature["geometry"]) for feature in collection["features"]]
        for geometry in self.shapes:
            shapely.prepare(geometry)
        self.tree = STRtree(self.shapes)

    def state_of(self, lon: float, lat: float) -> str | None:
        """The state code of the outline that holds the point, or None outside every outline."""
        found = sorted(int(i) for i in self.tree.query(Point(lon, lat), predicate="intersects"))
        return self.codes[found[0]] if found else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--fetch", action="store_true", help="query Overpass")
    source.add_argument("--overpass", type=Path, help="a saved Overpass JSON response")
    parser.add_argument("--out", type=Path, default=BOUNDARIES)
    parser.add_argument("--tolerance", type=float, default=SIMPLIFY_DEGREES, help="simplification, in degrees")
    args = parser.parse_args(argv)
    if args.fetch:
        response = fetch_overpass()
    else:
        with open(args.overpass, encoding="utf-8") as f:
            response = json.load(f)
    collection = from_overpass(response, args.tolerance)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(collection, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    codes = ", ".join(f["properties"]["state"] for f in collection["features"])
    print(f"{len(collection['features'])} outlines ({codes}) written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
