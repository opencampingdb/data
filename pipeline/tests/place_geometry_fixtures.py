"""Fixtures shared by the place geometry tests: the OSM file, a water polygons
shapefile and a build of them (pipeline.place_geometry)."""

import json
from pathlib import Path

import shapefile

from pipeline.place_geometry import build

OSM = Path(__file__).parent / "fixtures" / "place_geometry.osm"

# The sea east of the coast at 151.35, in two 0.1 degree tiles once clipped
# to the extract's bounds (which end at 151.5), and a piece of sea elsewhere.
SEA = [
    [(151.35, -33.1), (151.35, -33.0), (151.6, -33.0), (151.6, -33.1), (151.35, -33.1)],
    [(10.0, 50.0), (10.0, 50.1), (10.1, 50.1), (10.1, 50.0), (10.0, 50.0)],
]


def water_polygons(folder: Path, rings=SEA) -> Path:
    """A water polygons shapefile like OSM's split WGS 84 file, one ring per polygon."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "water_polygons.shp"
    with shapefile.Writer(str(path), shapeType=shapefile.POLYGON) as w:
        w.field("x", "N")
        w.field("y", "N")
        for ring in rings:
            w.poly([ring])
            w.record(0, 0)
    return path


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def run(tmp_path: Path, osm: Path = OSM, previous: Path | None = None, name: str = "out") -> Path:
    out = tmp_path / name
    build(osm, water_polygons(tmp_path / "wp"), out, previous_tiles=previous)
    return out


def tiles(out: Path) -> dict[str, list[dict]]:
    root = out / "tiles"
    return {str(p.relative_to(root)): lines(p) for p in sorted(root.rglob("*.ndjson"))}
