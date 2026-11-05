"""Shapes and roads the duplicate rules need, kept beside the extract.

pipeline.extract --geometry writes this sidecar. It is read by
pipeline.duplicates, pipeline.sync, pipeline.maproulette and pipeline.split,
and never goes into place records or a release. Each line is one JSON object:

    {"area": "way/123", "polygons": [[[[lon, lat], ...], ...], ...]}
        The outline of a place that is an area (a closed way or a
        multipolygon), simplified to about half a metre. Each polygon is an
        outer ring followed by its holes.
    {"signals": "node/123", "operator": "...", "bound": ["north"]}
        Tags that tell two places apart: the operator, and the directions a
        name, description or direction tag gives with "northbound",
        "south bound" and so on.
    {"road": 123, "highway": "trunk", "name": "...", "ref": "...", "coords": [[lon, lat], ...]}
        A piece of a major road near a place (see MAJOR_HIGHWAYS). Only the
        parts within about a kilometre of a place are kept.

Lines are sorted, so the same input always gives the same file.

Two objects of one place, or two places? The rules, in order:

1. Same place by area. When one of them is an area of the other's group,
   and the other lies inside it, on its edge, or within EDGE_M of its edge
   with no major road between, they are the same place. Rules 2 and 3 are
   not asked: a renamed operator on one of them does not make two places.
2. A major road between them. Two objects within the duplicate distance
   (pipeline.duplicates.MAX_DISTANCE_M) are separate places when the straight
   line between them crosses a major road. For an area, the line runs from
   the nearest point of its outline.
3. Never-merge signals. Different names (once generic words are dropped),
   different directions (northbound and southbound), different operators or
   different entry rules (access=*) keep two objects apart. These signals
   only ever keep places apart. They never make two places one.
"""

import json
import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import shapely
from shapely import MultiPolygon, Point, Polygon
from shapely.ops import nearest_points

from .common import KIND_GROUP, write_text

# Roads that split a place in two. Any of these, whatever its oneway tag.
MAJOR_HIGHWAYS = frozenset({
    "motorway", "trunk", "primary", "secondary",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
})
# A tertiary road counts only when it is one way (one carriageway of a divided road).
ONEWAY_HIGHWAYS = MAJOR_HIGHWAYS | {"tertiary"}
ONEWAY_VALUES = frozenset({"yes", "-1"})

# Road grid cell, in degrees (about 1.1 km north to south).
CELL = 0.01

# An object this close to an area's edge, with no major road between, is
# part of the area's place (rule 1).
EDGE_M = 20.0

# Outlines are simplified to this tolerance, in degrees (about half a metre).
SIMPLIFY = 0.000005

EARTH_M = 6_371_008.8

BOUND_RE = re.compile(r"\b(north|south|east|west)[\s-]*bound\b", re.IGNORECASE)
BOUND_TAGS = ("name", "description", "direction")


def is_major_road(tags) -> bool:
    """True for a way that splits a place in two (rule 2)."""
    highway = tags.get("highway")
    if highway in MAJOR_HIGHWAYS:
        return True
    return highway in ONEWAY_HIGHWAYS and tags.get("oneway") in ONEWAY_VALUES


def bounds_in(tags) -> list[str]:
    """Directions such as "north" named by northbound or "south bound" in the tags, sorted."""
    found = set()
    for key in BOUND_TAGS:
        for match in BOUND_RE.finditer(tags.get(key, "")):
            found.add(match.group(1).lower())
    return sorted(found)


def signals_from_tags(tags) -> dict:
    """The never-merge signals in a place's tags, or {} when it has none."""
    signals = {}
    operator = tags.get("operator", "").strip()
    if operator:
        signals["operator"] = operator
    bound = bounds_in(tags)
    if bound:
        signals["bound"] = bound
    return signals


def cell_of(lat: float, lon: float) -> tuple[int, int]:
    return math.floor(lat / CELL), math.floor(lon / CELL)


def cells_around(lat: float, lon: float) -> set[tuple[int, int]]:
    row, col = cell_of(lat, lon)
    return {(row + dr, col + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)}


def segment_cells(x1: float, y1: float, x2: float, y2: float):
    """Every grid cell the bounding box of a segment touches."""
    row1, col1 = cell_of(min(y1, y2), min(x1, x2))
    row2, col2 = cell_of(max(y1, y2), max(x1, x2))
    for row in range(row1, row2 + 1):
        for col in range(col1, col2 + 1):
            yield row, col


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lon, lat) points, in metres."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_M * math.asin(math.sqrt(h))


def _orientation(ax, ay, bx, by, cx, cy) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def segments_cross(p, q, r, s) -> bool:
    """True when segment p-q and segment r-s cross at a point inside both.

    Touching at an end does not count: an object standing on the road's
    centre line is not on the other side of it.
    """
    d1 = _orientation(r[0], r[1], s[0], s[1], p[0], p[1])
    d2 = _orientation(r[0], r[1], s[0], s[1], q[0], q[1])
    d3 = _orientation(p[0], p[1], q[0], q[1], r[0], r[1])
    d4 = _orientation(p[0], p[1], q[0], q[1], s[0], s[1])
    return ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0))


# ------------------------------------------------------------------ writing


class SidecarBuilder:
    """Collects what the sidecar holds during pipeline.extract's read."""

    def __init__(self) -> None:
        self.areas: dict[str, list] = {}
        self.signals: dict[str, dict] = {}
        # Every major road in the file: (id, highway, name, ref, [(lon, lat), ...]).
        self.roads: list[tuple] = []

    def road(self, way) -> None:
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
            self.roads.append((
                way.id, tags.get("highway"), tags.get("name"), tags.get("ref"), coords,
            ))

    def place(self, ref: str, tags, geometry) -> None:
        """Notes a place's outline (for an area) and its signals."""
        if geometry is not None and not geometry.is_empty:
            simple = shapely.simplify(geometry, SIMPLIFY, preserve_topology=True)
            self.areas[ref] = polygons_of(simple)
        signals = signals_from_tags(tags)
        if signals:
            self.signals[ref] = signals

    def lines(self, places: list[dict]) -> list[dict]:
        """The sidecar's lines, keeping only road pieces near a place."""
        wanted: set[tuple[int, int]] = set()
        for record in places:
            wanted |= cells_around(record["location"]["lat"], record["location"]["lon"])
        for polygons in self.areas.values():
            for polygon in polygons:
                xs = [x for x, _ in polygon[0]]
                ys = [y for _, y in polygon[0]]
                for row, col in segment_cells(min(xs), min(ys), max(xs), max(ys)):
                    wanted |= {(row + dr, col + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)}

        lines = [{"area": ref, "polygons": polygons} for ref, polygons in sorted(self.areas.items())]
        lines += [{"signals": ref, **signals} for ref, signals in sorted(self.signals.items())]
        for road_id, highway, name, ref, coords in sorted(self.roads, key=lambda r: r[0]):
            for piece in _pieces_near(coords, wanted):
                line = {"road": road_id, "highway": highway}
                if name:
                    line["name"] = name
                if ref:
                    line["ref"] = ref
                line["coords"] = [list(c) for c in piece]
                lines.append(line)
        return lines

    def write(self, path: Path, places: list[dict]) -> int:
        lines = self.lines(places)
        write_text(path, "".join(json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n" for line in lines))
        return len(lines)


def _pieces_near(coords: list[tuple], wanted: set) -> list[list[tuple]]:
    """Runs of a road's segments that touch a wanted cell."""
    pieces, current = [], []
    for a, b in zip(coords, coords[1:]):
        if any(cell in wanted for cell in segment_cells(a[0], a[1], b[0], b[1])):
            if not current:
                current = [a]
            current.append(b)
        elif current:
            pieces.append(current)
            current = []
    if current:
        pieces.append(current)
    return pieces


def polygons_of(geometry) -> list:
    """[[outer ring, *holes], ...] of a polygon or multipolygon, as rounded (lon, lat) lists."""
    if isinstance(geometry, Polygon):
        parts = [geometry]
    elif isinstance(geometry, MultiPolygon):
        parts = list(geometry.geoms)
    else:
        parts = [g for g in getattr(geometry, "geoms", []) if isinstance(g, Polygon)]
    out = []
    for polygon in parts:
        if polygon.is_empty:
            continue
        rings = [polygon.exterior] + list(polygon.interiors)
        out.append([[[round(x, 7), round(y, 7)] for x, y in ring.coords] for ring in rings])
    return out


# ------------------------------------------------------------------ reading


@dataclass
class Road:
    id: int
    highway: str
    name: str | None = None
    ref: str | None = None

    def label(self) -> str:
        """"Newell Highway (A39, trunk, way/123)"."""
        details = ", ".join(x for x in (self.ref, self.highway, f"way/{self.id}") if x)
        return f"{self.name} ({details})" if self.name else f"unnamed road ({details})"


@dataclass
class Geometry:
    """The sidecar, loaded. An empty one knows no areas, roads or signals."""

    areas: dict[str, object] = field(default_factory=dict)
    signals: dict[str, dict] = field(default_factory=dict)
    roads: list[Road] = field(default_factory=list)
    grid: dict[tuple[int, int], list[tuple]] = field(default_factory=dict)

    def add_road(self, road: Road, coords: list) -> None:
        index = len(self.roads)
        self.roads.append(road)
        for a, b in zip(coords, coords[1:]):
            for cell in segment_cells(a[0], a[1], b[0], b[1]):
                self.grid.setdefault(cell, []).append((a[0], a[1], b[0], b[1], index))

    def shape(self, record: dict):
        """The record's outline when it is an area, else its point."""
        area = self.areas.get(osm_ref(record))
        if area is not None:
            return area
        return Point(record["location"]["lon"], record["location"]["lat"])

    def road_between(self, p: tuple[float, float], q: tuple[float, float]) -> Road | None:
        """The first major road the straight line p-q crosses, or None. Points are (lon, lat)."""
        seen = set()
        for cell in segment_cells(p[0], p[1], q[0], q[1]):
            for x1, y1, x2, y2, index in self.grid.get(cell, ()):
                key = (x1, y1, x2, y2, index)
                if key in seen:
                    continue
                seen.add(key)
                if segments_cross(p, q, (x1, y1), (x2, y2)):
                    return self.roads[index]
        return None


def osm_ref(record: dict) -> str | None:
    osm = record.get("osm")
    return f"{osm['type']}/{osm['id']}" if osm else None


def load_geometry(path: Path | None) -> Geometry:
    """Read the sidecar. None gives an empty Geometry."""
    geometry = Geometry()
    if path is None:
        return geometry
    with open(path, encoding="utf-8") as f:
        for text in f:
            if not text.strip():
                continue
            line = json.loads(text)
            if "area" in line:
                polygons = [Polygon(p[0], p[1:]) for p in line["polygons"] if len(p[0]) >= 4]
                if polygons:
                    shape = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
                    if not shape.is_valid:
                        shape = shapely.make_valid(shape)
                    geometry.areas[line["area"]] = shape
            elif "signals" in line:
                geometry.signals[line["signals"]] = {k: v for k, v in line.items() if k != "signals"}
            elif "road" in line:
                road = Road(line["road"], line["highway"], line.get("name"), line.get("ref"))
                geometry.add_road(road, line["coords"])
    return geometry


# ------------------------------------------------------------------ the rules


@dataclass
class Verdict:
    """What the rules say about two objects.

    same: rule 1 says they are one place. separate: rule 2 or 3 says they
    are two, with reason ("road", "bound", "name", "operator" or "entry")
    and, for a road, the road. Neither: the rules have no view, and the
    usual duplicate tests decide. gap_m is the distance between their
    shapes (0 when one lies in the other), and line the (lon, lat) points
    it was measured between.
    """

    gap_m: float
    line: tuple
    same: bool = False
    separate: bool = False
    reason: str | None = None
    road: Road | None = None
    detail: str | None = None


def group_of(record: dict) -> str | None:
    """A record's group. Extract places carry only a kind."""
    return record.get("group") or KIND_GROUP.get(record.get("kind"))


def _names_differ(a: dict, b: dict) -> bool:
    from .duplicates import NAME_RATIO, differ_only_in_numbers, normalise_name

    name_a, name_b = normalise_name(a.get("name")), normalise_name(b.get("name"))
    if not name_a or not name_b:
        return False  # no name, or only generic words: no signal
    if differ_only_in_numbers(name_a, name_b):
        return True
    return SequenceMatcher(None, name_a, name_b).ratio() < NAME_RATIO


def _operator_key(operator: str) -> str:
    """Lower case, letters and digits only, without "the"."""
    words = re.sub(r"[^0-9a-z]+", " ", operator.casefold()).split()
    return "".join(word for word in words if word != "the")


def _operators_differ(a: str, b: str) -> bool:
    """Different operators, allowing for spacing, a missing "the", a typo or a longer form."""
    key_a, key_b = _operator_key(a), _operator_key(b)
    if not key_a or not key_b or key_a in key_b or key_b in key_a:
        return False
    return SequenceMatcher(None, key_a, key_b).ratio() < 0.8


def never_merge(a: dict, b: dict, geometry: Geometry) -> tuple[str, str] | None:
    """Rule 3: (reason, detail) when a signal keeps the two apart, else None."""
    if _names_differ(a, b):
        return "name", f"names differ: {a.get('name')} / {b.get('name')}"
    sa, sb = geometry.signals.get(osm_ref(a), {}), geometry.signals.get(osm_ref(b), {})
    # The name is in every record, so its direction counts without the sidecar too.
    bound_a = sorted(set(sa.get("bound", [])) | set(bounds_in({"name": a.get("name") or ""})))
    bound_b = sorted(set(sb.get("bound", [])) | set(bounds_in({"name": b.get("name") or ""})))
    if bound_a and bound_b and bound_a != bound_b:
        return "bound", f"directions differ: {'/'.join(bound_a)}bound and {'/'.join(bound_b)}bound"
    if sa.get("operator") and sb.get("operator") and _operators_differ(sa["operator"], sb["operator"]):
        return "operator", f"operators differ: {sa['operator']} / {sb['operator']}"
    ea, eb = a.get("facts", {}).get("entry"), b.get("facts", {}).get("entry")
    if ea and eb and ea != eb:
        return "entry", f"access differs: {ea} / {eb}"
    return None


def judge(a: dict, b: dict, geometry: Geometry, max_distance_m: float) -> Verdict:
    """Apply the three rules to two records (or extract places) with location and osm."""
    shape_a, shape_b = geometry.shape(a), geometry.shape(b)
    pa, pb = nearest_points(shape_a, shape_b)
    line = ((pa.x, pa.y), (pb.x, pb.y))
    gap = 0.0 if shape_a.intersects(shape_b) else metres(*line)
    verdict = Verdict(gap_m=gap, line=line)

    is_area = osm_ref(a) in geometry.areas or osm_ref(b) in geometry.areas
    if is_area and group_of(a) == group_of(b):
        if gap == 0.0 or (gap <= EDGE_M and geometry.road_between(*line) is None):
            verdict.same = True
            return verdict

    if gap == 0.0 and is_area:
        road = None  # one lies in the other: no road can be between them
    elif gap <= max_distance_m:
        road = geometry.road_between(*line)
    else:
        road = None
    if road is not None:
        verdict.separate, verdict.reason, verdict.road = True, "road", road
        verdict.detail = f"crosses {road.label()}"
        return verdict
    signal = never_merge(a, b, geometry)
    if signal is not None:
        verdict.separate, (verdict.reason, verdict.detail) = True, signal
    return verdict
