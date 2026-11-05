"""Build the release files: places.ndjson.gz, reviews.ndjson.gz, fees.ndjson.gz,
merged.ndjson.gz, commons.ndjson.gz and manifest.json, and the place index for
feedback intakes.

Usage:
    python -m pipeline.release --root . --out dist [--repository opencampingdb/data]

The data version and the generated time default to the current git commit
(its commit time and short hash), so building the same commit twice gives
byte-identical files. Both can be set by hand with --data-version and
--generated.

Released records carry the facts set by the rules in schema/fact_rules.json,
with the source rule and the rule's name in fact_rules, wherever no other
source gives a value. Place files in places/ never hold them.

place-index.ndjson.gz is not a release file and the manifest leaves it out.
It holds one line per released place, sorted by id: {"cell", "fact_dates",
"fact_sources", "facts", "id"}, where cell is the whole-degree map cell
[floor(lon), floor(lat)]. Its facts include the rule facts. pipeline.place_index loads it into a feedback intake,
which decides feedback against it at send time.

The release tag is data-<commit time>, for example data-20261030T160000Z.
The manifest names it, and gives each file an absolute URL in that release,
so a manifest never points at files that have since changed.
"""

import argparse
import gzip
import hashlib
import io
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .commons import load_do_not_match, without_listed
from .common import (
    ATTRIBUTION,
    LICENCE,
    dump_line,
    is_visible,
    iter_fee_files,
    iter_review_files,
    load_blocked,
    load_places,
    with_rule_facts,
    write_text,
)

# 1: the first record format. 2: kinds are the OTWD vocabulary and records
# carry a group. 3: osm.timestamp and fact_dates say how fresh a place is.
# 4: last_confirmed, when a traveller last found the place there.
# 5: reviews carry contributor and visited_nearby, and hidden reviews and
# reviews from blocked contributors are left out.
# 6: fees.ndjson.gz, what travellers paid per night in the last 24 months.
# 7: site features, more kinds, and fee may be "donation".
# 8: photos.ndjson.gz. 9: photos.ndjson.gz gone again: Open Camping DB holds no photos.
# 10: places travellers added carry standing, and held and hidden ones are left out.
# 11: fact_sources may be community_report.
# 11.1: commons.ndjson.gz, freely licensed Wikimedia Commons photos matched to places to stay.
# 12: fact_sources may be rule, with the rule's name in fact_rules (schema/fact_rules.json).
# Raise FORMAT_VERSION only when a reader of the previous format would misread the
# records: a field removed or renamed, a meaning changed, or a new value in an
# existing field. Announce it in an issue first (CONNECTING.md section 1). An
# additive change (a new optional field or file, records left out, a new kind)
# raises FORMAT_MINOR instead, and FORMAT_MINOR goes back to 0 when FORMAT_VERSION rises.
FORMAT_VERSION = 12
FORMAT_MINOR = 0
# New places with these standings wait for a person or were taken down.
UNRELEASED_STANDINGS = {"held", "hidden"}
# Fees paid more than this many months before the release are left out.
FEE_MONTHS = 24
DEFAULT_REPOSITORY = "opencampingdb/data"
PLACE_INDEX_FILE = "place-index.ndjson.gz"


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
    """Every place record except merged ones (tombstones) and held or hidden new places, one per line, sorted by id.

    Each record carries the facts its kind's rules set (pipeline.common.with_rule_facts).
    """
    places = load_places(root)
    lines = [
        dump_line(with_rule_facts(places[place_id])) + "\n"
        for place_id in sorted(places)
        if places[place_id].get("status") != "merged"
        and places[place_id].get("standing") not in UNRELEASED_STANDINGS
    ]
    return "".join(lines).encode("utf-8"), len(lines)


def cell_of(location: dict) -> tuple[int, int]:
    """The whole-degree map cell a location is in. 180 east and 90 north fall in the last cell."""
    return min(math.floor(location["lon"]), 179), min(math.floor(location["lat"]), 89)


def place_index_ndjson(root: Path) -> tuple[bytes, int]:
    """One index line for each place places.ndjson.gz holds, sorted by id."""
    places = {place_id: with_rule_facts(record) for place_id, record in load_places(root).items()}
    lines = [
        dump_line({
            "id": place_id,
            "cell": list(cell_of(places[place_id]["location"])),
            "facts": places[place_id].get("facts", {}),
            "fact_sources": places[place_id].get("fact_sources", {}),
            "fact_dates": places[place_id].get("fact_dates", {}),
        }) + "\n"
        for place_id in sorted(places)
        if places[place_id].get("status") != "merged"
        and places[place_id].get("standing") not in UNRELEASED_STANDINGS
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
    """Every visible review, grouped by place in id order, each file's lines in their id order.

    Hidden reviews and reviews from blocked contributors are left out.
    """
    blocked = load_blocked(root)
    lines = []
    for path in iter_review_files(root):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    review = json.loads(line)
                    if is_visible(review, blocked):
                        lines.append(dump_line(review) + "\n")
    return "".join(lines).encode("utf-8"), len(lines)


def month_index(year_month: str) -> int:
    """Months since year 0 of a YYYY-MM value (or the start of an ISO date)."""
    return int(year_month[:4]) * 12 + int(year_month[5:7]) - 1


def fees_ndjson(root: Path, generated: str) -> tuple[bytes, int]:
    """Every fee paid within FEE_MONTHS months of the release time, grouped by
    place in id order, each file's lines in their id order.

    Fees from blocked contributors are left out.
    """
    blocked = load_blocked(root)
    earliest = month_index(generated) - FEE_MONTHS
    lines = []
    for path in iter_fee_files(root):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    fee = json.loads(line)
                    if month_index(fee["paid"]) >= earliest and fee.get("contributor") not in blocked:
                        lines.append(dump_line(fee) + "\n")
    return "".join(lines).encode("utf-8"), len(lines)


def commons_ndjson(root: Path, released: set[str]) -> tuple[bytes, int]:
    """One {"place", "files"} line per released place with a Commons match, sorted by place id.

    commons/matches.ndjson is written by pipeline.commons. Places checked with no
    match, places the release leaves out and the place and file pairs on
    moderation/commons-do-not-match.json are left out here too.
    """
    path = root / "commons" / "matches.ndjson"
    listed = load_do_not_match(root)
    lines = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    match = json.loads(line)
                    files = without_listed(match["place"], match["files"], listed)
                    if files and match["place"] in released:
                        lines.append((match["place"], dump_line({"place": match["place"], "files": files}) + "\n"))
    lines.sort()
    return "".join(line for _, line in lines).encode("utf-8"), len(lines)


def released_ids(root: Path) -> set[str]:
    places = load_places(root)
    return {
        place_id for place_id, record in places.items()
        if record.get("status") != "merged" and record.get("standing") not in UNRELEASED_STANDINGS
    }


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
    """Write the five data files, manifest.json and the place index into out_dir and return the manifest.

    release defaults to data-<timestamp> taken from data_version.
    """
    release = release or release_tag(data_version)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    places_raw, places_count = places_ndjson(root)
    reviews_raw, reviews_count = reviews_ndjson(root)
    fees_raw, fees_count = fees_ndjson(root, generated)
    merged_raw, merged_count = merged_ndjson(root)
    index_raw, _ = place_index_ndjson(root)
    commons_raw, commons_count = commons_ndjson(root, released_ids(root))
    (out_dir / PLACE_INDEX_FILE).write_bytes(gzip_bytes(index_raw))

    manifest = {
        "format_version": FORMAT_VERSION,
        "format_minor": FORMAT_MINOR,
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
        "fees": write_asset(
            out_dir, "fees.ndjson.gz", fees_raw, fees_count,
            asset_url(repository, release, "fees.ndjson.gz"),
        ),
        "merged": write_asset(
            out_dir, "merged.ndjson.gz", merged_raw, merged_count,
            asset_url(repository, release, "merged.ndjson.gz"),
        ),
        "commons": write_asset(
            out_dir, "commons.ndjson.gz", commons_raw, commons_count,
            asset_url(repository, release, "commons.ndjson.gz"),
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
