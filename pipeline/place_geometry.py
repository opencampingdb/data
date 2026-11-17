"""Water and major roads for the feedback engine's plausibility check.

    python -m pipeline.place_geometry --extract australia-latest.osm.pbf \\
        --water-polygons water-polygons-split-4326.zip \\
        [--previous-tiles tiles.json] --out build/

The feedback engine holds a new place pinned in the sea, in a lake or on a
major road's carriageway, and lets its reviewer merge two places only when
no major road runs between them. This command builds the files it reads,
in the formats its self-hosting guide gives under "Plausible locations"
(see "Place geometry" in pipeline/README.md).
"""

import argparse
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np
import osmium
import shapefile
import shapely
from shapely import MultiPolygon, Polygon

from .common import sha256_of
from .roads import MajorRoads
from .water import METRES_PER_DEGREE

# Lakes smaller than this, in square metres (1 ha), are left out.
MIN_LAKE_M2 = 10_000
# natural=water with one of these water values is not a lake.
NOT_LAKES = frozenset({"river", "canal", "stream", "ditch", "drain", "wastewater"})
# Nor is water tagged intermittent=yes: a dry salt lake such as Kati
# Thanda-Lake Eyre or Lake Torrens, where a camp on or beside the dry bed
# must not be held as pinned in water.
# Shapes are simplified to about 5 m, in degrees.
SIMPLIFY = 5 / METRES_PER_DEGREE
# Sea is kept only in tiles this many tiles (about 55 km) or fewer from a
# tile with land in it, so the open ocean inside the extract's bounds is
# not cut into tiles nobody pins a place in. A known limit: a pin further
# out than that is not held (see "Place geometry" in pipeline/README.md).
SEA_MARGIN_TILES = 5
# Coordinates are written to this many decimal places (about 0.1 m).
DECIMALS = 6

# The grid the shapes are cut along and the tiles cut from, in tiles per degree (0.1 degree).
TILES_PER_DEGREE = 10

# The intake does not read a tile larger than this (MAX_TILE_BYTES in the engine).
MAX_TILE_BYTES = 4 * 1024 * 1024
# A tile holds every line whose shape comes this close to it, in metres.
TILE_MARGIN_M = 100
# The engine's constants for the margin, so the tiles are cut by its rule.
MARGIN_METRES_PER_DEGREE_LAT = 110_574
MARGIN_METRES_PER_DEGREE_LON = 111_320

WKB = osmium.geom.WKBFactory()


def grid_index(degrees: float) -> int:
    """The row of a latitude or the column of a longitude on the tile grid."""
    return math.floor(degrees * TILES_PER_DEGREE)


def tile_cells(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[tuple[int, int]]:
    """(row, col) of every tile a shape with these bounds belongs in.

    The bounds are grown by TILE_MARGIN_M and every tile they touch is
    listed: the rule of tileKeys in the feedback engine's
    packages/intake/src/place-geometry.ts, worked the same way.
    """
    d_lat = TILE_MARGIN_M / MARGIN_METRES_PER_DEGREE_LAT
    widest = max(abs(min_lat), abs(max_lat)) + d_lat
    d_lon = TILE_MARGIN_M / (MARGIN_METRES_PER_DEGREE_LON * math.cos(min(widest, 89) * math.pi / 180))
    return [
        (row, col)
        for row in range(grid_index(min_lat - d_lat), grid_index(max_lat + d_lat) + 1)
        for col in range(grid_index(min_lon - d_lon), grid_index(max_lon + d_lon) + 1)
    ]


def tile_key(layer: str, cell: tuple[int, int]) -> str:
    return f"{layer}/{cell[0]}/{cell[1]}.ndjson"


def cell_box(row: int, col: int):
    return shapely.box(*(edge / TILES_PER_DEGREE for edge in (col, row, col + 1, row + 1)))


def cut(shape) -> list[tuple[tuple[int, int], object]]:
    """The pieces of a shape in each grid cell: ((row, col), piece), by cell."""
    minx, miny, maxx, maxy = shape.bounds
    shapely.prepare(shape)
    pieces = []
    for row in range(grid_index(miny), grid_index(maxy) + 1):
        for col in range(grid_index(minx), grid_index(maxx) + 1):
            box = cell_box(row, col)
            if not shape.intersects(box):
                continue
            piece = box if shape.contains(box) else shapely.intersection(shape, box)
            if piece.area > 0:
                pieces.append(((row, col), piece))
    return pieces


def dumps(line: dict) -> str:
    return json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def square_metres(geometry) -> float:
    """The area of a shape in lon, lat degrees, flattened at its centroid's latitude."""
    scale = math.cos(math.radians(geometry.centroid.y))
    return geometry.area * METRES_PER_DEGREE * METRES_PER_DEGREE * scale


def is_lake(tags) -> bool:
    return tags.get("natural") == "water" and tags.get("water") not in NOT_LAKES and tags.get("intermittent") != "yes"


def polygons_of(geometry) -> list:
    """[[outer ring, *holes], ...] of a polygonal shape, rounded to DECIMALS.

    A ring that rounding leaves with fewer than four points is dropped, and
    a polygon without its outer ring with it.
    """
    if isinstance(geometry, Polygon):
        parts = [geometry]
    else:
        parts = [g for g in getattr(geometry, "geoms", []) if isinstance(g, (Polygon, MultiPolygon))]
        parts = [p for g in parts for p in (g.geoms if isinstance(g, MultiPolygon) else [g])]
    out = []
    for polygon in parts:
        if polygon.is_empty:
            continue
        rings = []
        for ring in [polygon.exterior, *polygon.interiors]:
            points = []
            for x, y in ring.coords:
                point = [round(x, DECIMALS), round(y, DECIMALS)]
                if not points or points[-1] != point:
                    points.append(point)
            if len(points) >= 4:
                rings.append(points)
            elif not rings:
                break
        if rings:
            out.append(rings)
    return out


def extract_bounds(path: Path) -> tuple[float, float, float, float]:
    """(min lon, min lat, max lon, max lat) from the extract's header."""
    box = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING).header().box()
    if not box.valid():
        raise SystemExit(f"{path} has no bounds in its header")
    return box.bottom_left.lon, box.bottom_left.lat, box.top_right.lon, box.top_right.lat


def extract_timestamp(path: Path) -> str | None:
    """The extract's replication timestamp, when its header has one."""
    header = osmium.io.Reader(str(path), osmium.osm.osm_entity_bits.NOTHING).header()
    return header.get("osmosis_replication_timestamp") or None


def read_sea(path: Path, bounds: tuple[float, float, float, float]) -> list:
    """The water polygons within bounds, clipped to them, in the file's order."""
    frame = shapely.box(*bounds)
    sea = []
    with shapefile.Reader(str(path)) as reader:
        for shape in reader.iterShapes(bbox=list(bounds)):
            if shape is None or not shape.points:
                continue
            polygon = shapely.geometry.shape(shape.__geo_interface__)
            if not polygon.is_valid:
                polygon = shapely.make_valid(polygon)
            clipped = shapely.intersection(polygon, frame)
            if clipped.area > 0:
                sea.append(clipped)
    return sea


def read_extract(path: Path):
    """The lakes and major roads in an extract.

    Lakes are (name or None, shape), in the order read. Roads are a
    pipeline.roads.MajorRoads, the same rule and read as the sidecar's.
    """
    lakes = []
    roads = MajorRoads()
    processor = (
        osmium.FileProcessor(str(path))
        .with_locations()
        .with_areas(osmium.filter.TagFilter(("natural", "water")))
        .with_filter(osmium.filter.KeyFilter("natural", "highway"))
    )
    for obj in processor:
        if isinstance(obj, osmium.osm.Way):
            if "highway" in obj.tags:
                roads.way(obj)
        elif isinstance(obj, osmium.osm.Area) and is_lake(obj.tags):
            try:
                shape = shapely.from_wkb(WKB.create_multipolygon(obj))
            except (osmium.InvalidLocationError, RuntimeError):
                continue
            if not shape.is_valid:
                shape = shapely.make_valid(shape)
            if square_metres(shape) >= MIN_LAKE_M2:
                name = (obj.tags.get("name") or "").strip() or None
                lakes.append((name, shape))
    return lakes, roads


def water_pieces(shape) -> list[tuple[tuple[int, int], object]]:
    """A shape simplified, then cut along the grid: (cell, piece) for each piece.

    Simplifying first keeps the cut edges exactly on the grid lines, so the
    pieces either side of one meet.
    """
    return cut(shapely.simplify(shape, SIMPLIFY, preserve_topology=True))


def lines_of_piece(piece, fields: dict) -> list[dict]:
    """The water line of a piece, or none when rounding leaves nothing of it."""
    polygons = polygons_of(piece)
    return [{**fields, "polygons": polygons}] if polygons else []


def water_lines(shape, fields: dict) -> list[tuple[tuple[int, int], dict]]:
    """One line per piece of a shape cut along the grid, with its cell."""
    return [(cell, line) for cell, piece in water_pieces(shape) for line in lines_of_piece(piece, fields)]


def cells_near_land(sea: list[tuple[tuple[int, int], object]], bounds) -> set[tuple[int, int]]:
    """The cells within bounds that are SEA_MARGIN_TILES or fewer from a cell with land.

    A cell has land unless the sea covers all of it that lies within bounds.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    row0, col0 = grid_index(min_lat), grid_index(min_lon)
    rows, cols = math.ceil(max_lat * TILES_PER_DEGREE) - row0, math.ceil(max_lon * TILES_PER_DEGREE) - col0
    covered: dict[tuple[int, int], float] = {}
    for cell, piece in sea:
        covered[cell] = covered.get(cell, 0.0) + piece.area
    frame = shapely.box(*bounds)
    land = np.ones((rows, cols), dtype=bool)
    for (row, col), area in covered.items():
        inside = shapely.intersection(cell_box(row, col), frame).area
        if area >= inside * (1 - 1e-9):
            land[row - row0, col - col0] = False
    # Grow the land by the margin: a cell is kept when any cell within
    # SEA_MARGIN_TILES rows and columns of it has land.
    within_margin = land.copy()
    margin = SEA_MARGIN_TILES
    padded = np.pad(land, margin)
    for dr in range(-margin, margin + 1):
        for dc in range(-margin, margin + 1):
            within_margin |= padded[margin + dr : margin + dr + rows, margin + dc : margin + dc + cols]
    return {(int(r) + row0, int(c) + col0) for r, c in zip(*np.nonzero(within_margin))}


def road_line(road_id: int, highway: str, name: str | None, ref: str | None, coords: list) -> dict:
    """A road line in the geometry sidecar's shape."""
    line = {"road": road_id, "highway": highway}
    if name:
        line["name"] = name
    if ref:
        line["ref"] = ref
    line["coords"] = [[round(x, DECIMALS), round(y, DECIMALS)] for x, y in coords]
    return line


def water_tiles(water: list[dict]) -> dict[str, list[str]]:
    """Tile key to the water lines (as text) in that tile."""
    out: dict[str, list[str]] = {}
    for line in water:
        xs = [x for polygon in line["polygons"] for x, _ in polygon[0]]
        ys = [y for polygon in line["polygons"] for _, y in polygon[0]]
        text = dumps(line)
        for cell in tile_cells(min(xs), min(ys), max(xs), max(ys)):
            out.setdefault(tile_key("water", cell), []).append(text)
    return out


def road_tiles(roads: list[dict]) -> dict[str, list[str]]:
    """Tile key to the road lines (as text) in that tile.

    A tile holds the runs of a road's segments that come near it, so a long
    road is not copied whole into every tile it passes.
    """
    out: dict[str, list[str]] = {}
    for line in roads:
        coords = line["coords"]
        runs: dict[tuple[int, int], list[list]] = {}  # cell: [[point, ...], ...]
        for i, (a, b) in enumerate(zip(coords, coords[1:])):
            for cell in tile_cells(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])):
                pieces = runs.setdefault(cell, [])
                if pieces and pieces[-1][1] == i:
                    pieces[-1][1] = i + 1
                else:
                    pieces.append([i, i + 1])
        for cell, pieces in runs.items():
            for start, end in pieces:
                piece = {**line, "coords": coords[start : end + 1]}
                out.setdefault(tile_key("roads", cell), []).append(dumps(piece))
    return out


def write_tiles(root: Path, tiles: dict[str, list[str]]) -> dict[str, dict]:
    """Write each tile, its lines sorted. Returns the tile list: key to sha256 and bytes."""
    listing = {}
    for key in sorted(tiles):
        data = "".join(text + "\n" for text in sorted(tiles[key])).encode("utf-8")
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        listing[key] = {"sha256": sha256_of(data), "bytes": len(data)}
    return listing


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")


class TileTooLarge(Exception):
    """A tile over MAX_TILE_BYTES, which the intake would not read."""


def build(extract: Path, water_polygons: Path, out: Path, previous_tiles: Path | None = None) -> dict:
    """Build the water file, the roads file and the tiles into out. Returns the summary.

    Raises TileTooLarge, before writing the tile list, the changed list and
    the summary, when a tile is over MAX_TILE_BYTES.
    """
    out.mkdir(parents=True, exist_ok=True)
    for name in ("tiles.json", "changed.json", "summary.json"):
        (out / name).unlink(missing_ok=True)
    bounds = extract_bounds(extract)
    sea = [item for shape in read_sea(water_polygons, bounds) for item in water_pieces(shape)]
    coastal = cells_near_land(sea, bounds)
    water = [(cell, line) for cell, piece in sea if cell in coastal for line in lines_of_piece(piece, {"water": "sea"})]
    sea_lines = len(water)
    lakes, major_roads = read_extract(extract)
    for name, shape in lakes:
        water += water_lines(shape, {"water": "lake", **({"name": name} if name else {})})
    water.sort(key=lambda item: (item[0], dumps(item[1])))
    water = [line for _, line in water]
    roads = [road_line(*road) for road in sorted(major_roads.roads, key=lambda road: road[0])]
    water_text = "".join(dumps(line) + "\n" for line in water).encode("utf-8")
    roads_text = "".join(dumps(line) + "\n" for line in roads).encode("utf-8")
    (out / "water.ndjson").write_bytes(water_text)
    (out / "roads.ndjson").write_bytes(roads_text)

    root = out / "tiles"
    if root.exists():
        shutil.rmtree(root)
    listing = write_tiles(root, {**water_tiles(water), **road_tiles(roads)})
    largest = max(listing.items(), key=lambda item: item[1]["bytes"], default=(None, {"bytes": 0}))
    if largest[1]["bytes"] > MAX_TILE_BYTES:
        raise TileTooLarge(f"tile {largest[0]} is {largest[1]['bytes']} bytes, over {MAX_TILE_BYTES} bytes")

    previous = json.loads(previous_tiles.read_text())["tiles"] if previous_tiles else {}
    changes = {
        "put": sorted(key for key, tile in listing.items() if previous.get(key, {}).get("sha256") != tile["sha256"]),
        "delete": sorted(key for key in previous if key not in listing),
    }

    def layer(prefix: str) -> dict:
        found = [tile["bytes"] for key, tile in listing.items() if key.startswith(prefix)]
        return {"tiles": len(found), "tile_bytes": sum(found), "largest_tile_bytes": max(found, default=0)}

    summary = {
        "extract": {"path": extract.name, "timestamp": extract_timestamp(extract), "bounds": list(bounds)},
        "water": {
            "lines": len(water),
            "sea_lines": sea_lines,
            "lake_lines": len(water) - sea_lines,
            "lakes": len(lakes),
            "bytes": len(water_text),
            **layer("water/"),
        },
        "roads": {"roads": len(roads), "bytes": len(roads_text), **layer("roads/")},
        "tiles": {"count": len(listing), "put": len(changes["put"]), "delete": len(changes["delete"]), "largest": largest[0]},
    }
    write_json(out / "tiles.json", {"tiles": listing})
    write_json(out / "changed.json", changes)
    write_json(out / "summary.json", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--extract", type=Path, required=True)
    parser.add_argument("--water-polygons", type=Path, required=True)
    parser.add_argument("--previous-tiles", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        summary = build(args.extract, args.water_polygons, args.out, args.previous_tiles)
    except TileTooLarge as error:
        print(f"place geometry: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
