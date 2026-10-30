"""Build the release files: places.ndjson.gz, reviews.ndjson.gz and manifest.json.

Usage:
    python -m pipeline.release --root . --out dist [--repository opencampingdb/data]

The data version and the generated time default to the current git commit
(its commit time and short hash), so building the same commit twice gives
byte-identical files. Both can be set by hand with --data-version and
--generated.

The release tag is data-<commit time>, for example data-20261030T160000Z.
The manifest names it, and gives each file an absolute URL in that release,
so a manifest never points at files that have since changed.
"""

import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import ATTRIBUTION, LICENCE, dump_line, iter_review_files, load_places, write_text

# 1: the first record format. 2: kinds are the OTWD vocabulary and records
# carry a group.
FORMAT_VERSION = 2
DEFAULT_REPOSITORY = "opencampingdb/data"


def release_tag(data_version: str) -> str:
    """data-<timestamp> from a data_version of the form <timestamp>-<short sha>."""
    return "data-" + data_version.split("-")[0]


def asset_url(repository: str, tag: str, name: str) -> str:
    return f"https://github.com/{repository}/releases/download/{tag}/{name}"


def gzip_bytes(data: bytes) -> bytes:
    """Gzip with no file name and a zero timestamp, so equal input gives equal output."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as f:
        f.write(data)
    return buffer.getvalue()


def places_ndjson(root: Path) -> tuple[bytes, int]:
    """Every place record except merged ones (tombstones), one per line, sorted by id."""
    places = load_places(root)
    lines = [
        dump_line(places[place_id]) + "\n"
        for place_id in sorted(places)
        if places[place_id].get("status") != "merged"
    ]
    return "".join(lines).encode("utf-8"), len(lines)


def merged_ndjson(root: Path) -> tuple[bytes, int]:
    """One {"id", "merged_into"} line per tombstone, sorted by id, so apps can redirect old ids."""
    places = load_places(root)
    lines = [
        dump_line({"id": place_id, "merged_into": places[place_id]["merged_into"]}) + "\n"
        for place_id in sorted(places)
        if places[place_id].get("status") == "merged"
    ]
    return "".join(lines).encode("utf-8"), len(lines)


def reviews_ndjson(root: Path) -> tuple[bytes, int]:
    """Every review, grouped by place in id order, each file's lines in their id order."""
    lines = []
    for path in iter_review_files(root):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    lines.append(dump_line(json.loads(line)) + "\n")
    return "".join(lines).encode("utf-8"), len(lines)


def write_asset(out_dir: Path, name: str, raw: bytes, count: int, url: str) -> dict:
    """Gzip and write one data file, and return its manifest entry."""
    data = gzip_bytes(raw)
    (out_dir / name).write_bytes(data)
    return {
        "file": name,
        "url": url,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "count": count,
    }


def build_release(
    root: Path,
    out_dir: Path,
    data_version: str,
    generated: str,
    release: str | None = None,
    repository: str = DEFAULT_REPOSITORY,
) -> dict:
    """Write the three release files into out_dir and return the manifest.

    release defaults to data-<timestamp> taken from data_version.
    """
    release = release or release_tag(data_version)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    places_raw, places_count = places_ndjson(root)
    reviews_raw, reviews_count = reviews_ndjson(root)
    merged_raw, merged_count = merged_ndjson(root)

    manifest = {
        "format_version": FORMAT_VERSION,
        "data_version": data_version,
        "release": release,
        "generated": generated,
        "places": write_asset(
            out_dir, "places.ndjson.gz", places_raw, places_count,
            asset_url(repository, release, "places.ndjson.gz"),
        ),
        "reviews": write_asset(
            out_dir, "reviews.ndjson.gz", reviews_raw, reviews_count,
            asset_url(repository, release, "reviews.ndjson.gz"),
        ),
        "merged": write_asset(
            out_dir, "merged.ndjson.gz", merged_raw, merged_count,
            asset_url(repository, release, "merged.ndjson.gz"),
        ),
        "licence": LICENCE,
        "attribution": ATTRIBUTION,
    }
    write_text(
        out_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )
    return manifest


def git_version(root: Path) -> tuple[str, str]:
    """(data_version, generated) from the current commit in root."""
    out = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%ct %h"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    when = datetime.fromtimestamp(int(out[0]), timezone.utc)
    return f"{when:%Y%m%dT%H%M%SZ}-{out[1]}", f"{when:%Y-%m-%dT%H:%M:%SZ}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--data-version", help="default: <commit time>-<short sha>")
    parser.add_argument("--generated", help="default: commit time, ISO 8601 UTC")
    parser.add_argument(
        "--repository",
        default=DEFAULT_REPOSITORY,
        help=f"owner/name used in the asset URLs (default {DEFAULT_REPOSITORY})",
    )
    args = parser.parse_args(argv)

    data_version, generated = args.data_version, args.generated
    if not data_version or not generated:
        git_data_version, git_generated = git_version(args.root)
        data_version = data_version or git_data_version
        generated = generated or git_generated

    manifest = build_release(args.root, args.out, data_version, generated, repository=args.repository)
    # ASCII escapes keep the log readable on consoles that are not UTF-8.
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
