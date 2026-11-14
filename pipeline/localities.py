"""The nearest town to each place, and the council area it lies in.

The extract collects two kinds of OSM object in the same read as the places:

    locality  a node tagged place=city, place=town or place=village with a
              name of at most 100 characters
    lga       a boundary=administrative relation with admin_level=6 and a name of at most 100 characters: in
              Australia, a local government area (a council, shire, city or
              municipality, and the unincorporated areas OSM maps at that
              level)

For every place, set_facts records:

    locality  {"name": ..., "osm_id": ..., "type": ..., "distance_m": ...}
              for the nearest locality within LOCALITY_RADIUS_M of the
              place's location
    lga       {"name": ..., "osm_id": ...} for the local government area the
              place's location lies in

distance_m is the great circle distance from the place to the locality's
node, on the sphere of mean radius 6,371,008.8 m, rounded to 10 m (half up),
so a small move of either does not change the record. Ties go to the
locality with the lower OSM id. A point inside more than one lga (an
overlap in OSM's outlines) takes the one with the lower OSM id, so the same
file always gives the same answer.

rows() lists every locality, sorted by OSM id, with its own lga, for the
localities file (localities/localities.ndjson) that pipeline.database turns
into the localities table.
"""

import json
import math
import re
from pathlib import Path

import shapely
from shapely import STRtree

from .common import dump_line, write_text

LOCALITY_TYPES = ("city", "town", "village")
LOCALITY_RADIUS_M = 50_000
LGA_ADMIN_LEVEL = "6"
NAME_MAX = 100
EARTH_RADIUS_M = 6_371_008.8
METRES_PER_DEGREE = math.pi * EARTH_RADIUS_M / 180

# Pairs for osmium.filter.TagFilter. admin_level is checked in Python.
LOCALITY_TAG_PAIRS = tuple(("place", value) for value in LOCALITY_TYPES) + (("boundary", "administrative"),)

POPULATION = re.compile(r"^[0-9]{1,9}$")


def clean_name(tags: dict) -> str | None:
    """The object's name, or None when it has none or it is longer than NAME_MAX."""
    name = (tags.get("name") or "").strip()
    return name if name and len(name) <= NAME_MAX else None


def locality_type(tags: dict) -> str | None:
    """city, town or village for a named locality, else None."""
    place = tags.get("place")
    return place if place in LOCALITY_TYPES and clean_name(tags) else None


def is_lga(tags: dict) -> bool:
    """True for a named boundary=administrative, admin_level=6 object."""
    return (
        tags.get("boundary") == "administrative"
        and tags.get("admin_level", "").strip() == LGA_ADMIN_LEVEL
        and clean_name(tags) is not None
    )


def parse_population(value: str | None) -> int | None:
    """A population tag as a whole number, with thousands separators allowed, else None."""
    if not value:
        return None
    digits = re.sub(r"[,\s]", "", value)
    return int(digits) if POPULATION.match(digits) else None


def great_circle_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Metres between two points on the sphere (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def round_10(metres: float) -> int:
    """Metres rounded to 10, half up."""
    return (round(metres) + 5) // 10 * 10


class Localities:
    """The localities and local government areas found in a read."""

    def __init__(self) -> None:
        # (osm_id, name, type, lat, lon, population or None)
        self.towns: list[tuple] = []
        # (osm_id, name, geometry)
        self.lgas: list[tuple] = []
        self.town_tree: STRtree | None = None
        self.lga_tree: STRtree | None = None

    def add_town(self, osm_id: int, tags: dict, lat: float, lon: float) -> None:
        kind = locality_type(tags)
        if kind is None:
            return
        self.towns.append((osm_id, clean_name(tags), kind, lat, lon, parse_population(tags.get("population"))))
        self.town_tree = None

    def add_lga(self, osm_id: int, tags: dict, geometry) -> None:
        if geometry is None or geometry.is_empty or not is_lga(tags):
            return
        self.lgas.append((osm_id, clean_name(tags), geometry))
        self.lga_tree = None

    def counts(self) -> dict:
        return {"localities": len(self.towns), "lgas": len(self.lgas)}

    def lgas_of(self, points: list[tuple[float, float]]) -> list[dict | None]:
        """For each (lat, lon) point, {"name", "osm_id"} of the lga it lies in, or None."""
        found: list[dict | None] = [None] * len(points)
        if not points or not self.lgas:
            return found
        if self.lga_tree is None:
            self.lga_tree = STRtree([geometry for _, _, geometry in self.lgas])
        query = shapely.points([(lon, lat) for lat, lon in points])
        point_index, lga_index = self.lga_tree.query(query, predicate="intersects")
        best: dict[int, int] = {}
        for p, g in zip(point_index.tolist(), lga_index.tolist()):
            if p not in best or self.lgas[g][0] < self.lgas[best[p]][0]:
                best[p] = g
        for p, g in best.items():
            osm_id, name, _ = self.lgas[g]
            found[p] = {"name": name, "osm_id": osm_id}
        return found

    def nearest(self, lat: float, lon: float, radius_m: float = LOCALITY_RADIUS_M) -> tuple[tuple, float] | None:
        """(town, metres) for the nearest locality within radius_m, or None."""
        if not self.towns:
            return None
        if self.town_tree is None:
            self.town_tree = STRtree(shapely.points([(lon, lat) for _, _, _, lat, lon, _ in self.towns]))
        dlat = radius_m / METRES_PER_DEGREE
        dlon = radius_m / (METRES_PER_DEGREE * max(math.cos(math.radians(lat)), 0.01))
        box = shapely.box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
        best = None
        for index in self.town_tree.query(box).tolist():
            town = self.towns[index]
            metres = great_circle_m(lat, lon, town[3], town[4])
            if metres > radius_m:
                continue
            if best is None or (metres, town[0]) < (best[1], best[0][0]):
                best = (town, metres)
        return best

    def set_facts(self, records: list[dict]) -> None:
        """Set locality and lga on every place record."""
        lgas = self.lgas_of([(r["location"]["lat"], r["location"]["lon"]) for r in records])
        for record, lga in zip(records, lgas):
            facts = record["facts"]
            found = self.nearest(record["location"]["lat"], record["location"]["lon"])
            if found is not None:
                (osm_id, name, kind, _, _, _), metres = found
                facts["locality"] = {"name": name, "osm_id": osm_id, "type": kind, "distance_m": round_10(metres)}
            if lga is not None:
                facts["lga"] = lga

    def rows(self) -> list[dict]:
        """Every locality, sorted by OSM id, for the localities file."""
        towns = sorted(self.towns)
        lgas = self.lgas_of([(lat, lon) for _, _, _, lat, lon, _ in towns])
        rows = []
        for (osm_id, name, kind, lat, lon, population), lga in zip(towns, lgas):
            row = {"osm_id": osm_id, "name": name, "type": kind, "lat": round(lat, 6), "lon": round(lon, 6)}
            if population is not None:
                row["population"] = population
            if lga is not None:
                row["lga"] = lga
            rows.append(row)
        return rows


LOCALITIES_FILE = Path("localities") / "localities.ndjson"


def write_localities(path: Path, rows: list[dict]) -> None:
    write_text(path, "".join(dump_line(row) + "\n" for row in rows))


def load_localities(root: Path) -> list[dict]:
    """The rows of localities/localities.ndjson under root, or [] when there is no file yet."""
    path = Path(root) / LOCALITIES_FILE
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
