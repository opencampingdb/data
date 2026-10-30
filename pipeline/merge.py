"""Merge two records of the same place. Run by a person, never by a workflow.

Usage:
    python -m pipeline.merge <keep_id> <drop_id> --root .

What it does:
- The kept record gains merged_osm: the dropped record's OSM link, and any
  OSM links already merged into the dropped record, sorted. The OSM sync
  matches these objects to the kept record from then on, so the dropped
  place is never created again.
- Facts the dropped record got from feedback or contributions move to the
  kept record where it has no value for them. OSM facts do not move: the
  kept record has its own.
- The dropped record's reviews move to the kept record, which gets a new
  rating from all its reviews.
- The dropped record stays as a tombstone: status "merged" and merged_into
  set to the kept id, nothing else changed. Releases leave tombstones out of
  places.ndjson.gz and list them in merged.ndjson.gz, so apps that stored the
  old id can follow it.
- Older tombstones that pointed at the dropped record now point at the kept
  one, so every redirect takes one step.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

from .common import (
    FACT_KEYS,
    dump_line,
    dump_record,
    load_places,
    place_path,
    rating_mean,
    review_path,
    utc_now,
    write_text,
)

# Sources whose facts move to the kept record.
MOVABLE_SOURCES = ("feedback", "contributed")


class MergeError(Exception):
    """The merge was refused. Nothing was written."""


def read_reviews(root: Path, place_id: str) -> list[dict]:
    path = review_path(root, place_id)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def osm_links(record: dict) -> set[tuple[str, int]]:
    """The record's own OSM link and every link merged into it."""
    links = {(m["type"], m["id"]) for m in record.get("merged_osm", [])}
    if record.get("osm"):
        links.add((record["osm"]["type"], record["osm"]["id"]))
    return links


def merge(root: Path, keep_id: str, drop_id: str, now: str | None = None) -> dict:
    """Merge drop_id into keep_id. Returns the kept record."""
    root = Path(root)
    now = now or utc_now()
    places = load_places(root)

    if keep_id == drop_id:
        raise MergeError("keep and drop are the same record")
    for place_id in (keep_id, drop_id):
        if place_id not in places:
            raise MergeError(f"no such place {place_id}")
        if places[place_id].get("status") == "merged":
            raise MergeError(f"{place_id} is already merged into {places[place_id]['merged_into']}")

    dropped = places[drop_id]
    kept = copy.deepcopy(places[keep_id])

    # OSM links. The kept record's own link is not repeated in merged_osm.
    own = {(kept["osm"]["type"], kept["osm"]["id"])} if kept.get("osm") else set()
    links = (osm_links(kept) | osm_links(dropped)) - own
    if links:
        kept["merged_osm"] = [{"type": t, "id": i} for t, i in sorted(links)]

    # Facts from feedback and contributions, where the kept record has none.
    for key, source in sorted(dropped.get("fact_sources", {}).items()):
        if key in FACT_KEYS and source in MOVABLE_SOURCES and key not in kept["facts"]:
            if key in dropped["facts"]:
                kept["facts"][key] = dropped["facts"][key]
                kept["fact_sources"][key] = source

    # Reviews.
    moved = [dict(r, place=keep_id) for r in read_reviews(root, drop_id)]
    reviews = sorted(read_reviews(root, keep_id) + moved, key=lambda r: r["id"])
    if reviews:
        stars = [r["stars"] for r in reviews]
        kept["rating"] = {"count": len(stars), "mean": rating_mean(stars)}
        write_text(review_path(root, keep_id), "".join(dump_line(r) + "\n" for r in reviews))
    if moved:
        review_path(root, drop_id).unlink()

    kept["updated"] = now
    write_text(place_path(root, keep_id), dump_record(kept))

    tombstone = dict(dropped, status="merged", merged_into=keep_id)
    write_text(place_path(root, drop_id), dump_record(tombstone))

    for record in places.values():
        if record.get("merged_into") == drop_id:
            write_text(place_path(root, record["id"]), dump_record(dict(record, merged_into=keep_id)))

    return kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("keep_id")
    parser.add_argument("drop_id")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    args = parser.parse_args(argv)
    try:
        kept = merge(args.root, args.keep_id, args.drop_id)
    except MergeError as exc:
        print(f"Not merged: {exc}", file=sys.stderr)
        return 1
    print(f"Merged {args.drop_id} into {args.keep_id} ({kept['name'] or 'unnamed'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
