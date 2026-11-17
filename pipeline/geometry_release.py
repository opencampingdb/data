"""Check a place geometry build and pack it for the geometry release.

    python -m pipeline.geometry_release --build build/ --out publish/ \\
        --extract-source "Geofabrik" --water-polygons-modified 2026-11-10T03:12:00Z \\
        [--previous-manifest previous/manifest.json]

Checks a build from pipeline.place_geometry, then writes to the out folder
the files the geometry release holds: water.ndjson.gz, roads.ndjson.gz,
tiles.json, summary.json and manifest.json. A build that fails a check is
refused with exit code 1 and nothing is written, so the release keeps its
last good files. See "Geometry workflow" in pipeline/README.md.
"""

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import sha256_of

WATER_POLYGONS_URL = "https://osmdata.openstreetmap.de/download/water-polygons-split-4326.zip"
# A build with fewer than this share of any count in the previous release's
# manifest is refused, so a broken source never replaces a good release.
MIN_SHARE_OF_PREVIOUS = 0.5
# The counts that must be above zero and are compared with the previous release.
COUNTS = (("water", "sea_lines"), ("water", "lake_lines"), ("roads", "roads"), ("tiles", "count"))
FILES = ("water.ndjson", "roads.ndjson")


class BuildRejected(Exception):
    """A build that fails a check and must not be published."""


def read_json(path: Path):
    if not path.is_file():
        raise BuildRejected(f"{path.name} is missing")
    return json.loads(path.read_text())


def check(build: Path, previous_manifest: Path | None = None) -> dict:
    """Raise BuildRejected unless the build is whole and in step. Returns its summary."""
    summary = read_json(build / "summary.json")
    listing = read_json(build / "tiles.json")["tiles"]
    changed = read_json(build / "changed.json")
    for name in FILES:
        if not (build / name).is_file():
            raise BuildRejected(f"{name} is missing")

    for layer, field in COUNTS:
        if not summary.get(layer, {}).get(field):
            raise BuildRejected(f"the build has no {layer} {field}")
    if previous_manifest:
        previous = json.loads(previous_manifest.read_text())
        for layer, field in COUNTS:
            before = previous.get(layer, {}).get(field) or 0
            now = summary[layer][field]
            if now < before * MIN_SHARE_OF_PREVIOUS:
                raise BuildRejected(f"{layer} {field} fell from {before} to {now}, under {MIN_SHARE_OF_PREVIOUS:.0%} of the previous release")

    root = build / "tiles"
    on_disk = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()} if root.is_dir() else set()
    for key, tile in listing.items():
        if key not in on_disk:
            raise BuildRejected(f"tile {key} is in tiles.json but missing from tiles/")
        data = (root / key).read_bytes()
        if sha256_of(data) != tile["sha256"] or len(data) != tile["bytes"]:
            raise BuildRejected(f"tile {key} does not match its sha256 and bytes in tiles.json")
    extra = sorted(on_disk - set(listing))
    if extra:
        raise BuildRejected(f"{len(extra)} tiles are in tiles/ but not in tiles.json, such as {extra[0]}")
    stray = sorted(set(changed["put"]) - set(listing))
    if stray:
        raise BuildRejected(f"changed.json would put {stray[0]}, which is not in tiles.json")
    kept = sorted(set(changed["delete"]) & set(listing))
    if kept:
        raise BuildRejected(f"changed.json would delete {kept[0]}, which is still in tiles.json")
    return summary


def gzip_file(source: Path, target: Path) -> None:
    """Gzip with no name or time in the header, so the same file packs to the same bytes."""
    with source.open("rb") as raw, target.open("wb") as out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as packed:
            while chunk := raw.read(1 << 20):
                packed.write(chunk)


def pack(
    build: Path,
    out: Path,
    *,
    extract_source: str,
    water_polygons_modified: str | None,
    previous_manifest: Path | None = None,
) -> dict:
    """Check the build, then write the release files to out. Returns the manifest."""
    summary = check(build, previous_manifest)
    out.mkdir(parents=True, exist_ok=True)
    assets = {}
    for name in FILES:
        target = out / f"{name}.gz"
        gzip_file(build / name, target)
        assets[target.name] = {
            "bytes": target.stat().st_size,
            "sha256": sha256_of(target),
            "ndjson_bytes": (build / name).stat().st_size,
        }
    for name in ("tiles.json", "summary.json"):
        (out / name).write_bytes((build / name).read_bytes())
    manifest = {
        "release": "geometry",
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {
            "extract": {"source": extract_source, **summary["extract"]},
            "water_polygons": {"url": WATER_POLYGONS_URL, "last_modified": water_polygons_modified},
        },
        "assets": assets,
        "water": summary["water"],
        "roads": summary["roads"],
        "tiles": summary["tiles"],
        "licence": "ODbL-1.0",
        "attribution": "© OpenStreetMap contributors",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--extract-source", required=True, help="where the extract came from, for the manifest")
    parser.add_argument("--water-polygons-modified", help="the water polygons file's Last-Modified, as ISO 8601 UTC")
    parser.add_argument("--previous-manifest", type=Path, help="the manifest of the release being replaced")
    args = parser.parse_args(argv)
    try:
        manifest = pack(
            args.build,
            args.out,
            extract_source=args.extract_source,
            water_polygons_modified=args.water_polygons_modified,
            previous_manifest=args.previous_manifest,
        )
    except BuildRejected as error:
        print(f"geometry release: {error}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
