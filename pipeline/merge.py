"""Merge two records of the same place. Run by a person, never by a workflow.

Usage:
    python -m pipeline.merge <keep_id> <drop_id> --root . [--part]

What it does:
- The kept record gains merged_osm: the dropped record's OSM link, and any
  OSM links already merged into the dropped record, sorted. The OSM sync
  matches these objects to the kept record from then on, so the dropped
  place is never created again.
- --part says the two OSM objects are parts of one place drawn apart on
  purpose, such as separate camping areas of one ground, rather than the
  same place mapped twice. The dropped record's own link gets "part": true
  in merged_osm, and the MapRoulette double mapping challenge leaves it out.
  Part flags already on either record are kept.
- Facts the dropped record got from feedback or contributions move to the
  kept record where it has no value for them, with their fact_dates. OSM
  facts do not move: the kept record has its own.
- The kept record takes the newer last_confirmed of the two.
- The dropped record's reviews move to the kept record, which gets a new
  rating from all its visible reviews (not hidden, not from a blocked
  contributor).
- The dropped record's fees move to the kept record. Where one contributor
  has a fee for the same site type at both, only the newer is kept.
- The dropped record stays as a tombstone: status "merged" and merged_into
  set to the kept id, nothing else changed. Releases leave tombstones out of
  places.ndjson.gz and list them in merged.ndjson.gz, so apps that stored the
  old id can follow it.
- Older tombstones that pointed at the dropped record now point at the kept
  one, so every redirect takes one step.

A pair on moderation/distinct_places.json is refused: a person or
pipeline.split found them to be separate places. With --part it is allowed,
for two parts of one place that a road runs between, such as the two
sections of a caravan park on either side of a street.
"""

import argparse
import copy
import csv
import io
import json
import sys
from pathlib import Path

from .common import (
    FACT_KEYS,
    ROOFED_KINDS,
    dump_line,
    dump_record,
    fee_path,
    is_visible,
    load_blocked,
    load_distinct,
    load_places,
    place_path,
    rating_mean,
    review_path,
    utc_now,
    write_text,
)
from .duplicates import automatic_merge, distance_m, find_duplicates
from .geometry import Geometry, load_geometry

# Sources whose facts move to the kept record.
MOVABLE_SOURCES = ("feedback", "contributed", "community_report")


class MergeError(Exception):
    """The merge was refused. Nothing was written."""


def read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_reviews(root: Path, place_id: str) -> list[dict]:
    return read_lines(review_path(root, place_id))


def read_fees(root: Path, place_id: str) -> list[dict]:
    return read_lines(fee_path(root, place_id))


def osm_links(record: dict) -> set[tuple[str, int]]:
    """The record's own OSM link and every link merged into it."""
    links = {(m["type"], m["id"]) for m in record.get("merged_osm", [])}
    if record.get("osm"):
        links.add((record["osm"]["type"], record["osm"]["id"]))
    return links


def part_links(record: dict) -> set[tuple[str, int]]:
    """Links in merged_osm marked as a part drawn apart on purpose."""
    return {(m["type"], m["id"]) for m in record.get("merged_osm", []) if m.get("part")}


def merge(
    root: Path, keep_id: str, drop_id: str, now: str | None = None, part: bool = False, places: dict | None = None
) -> dict:
    """Merge drop_id into keep_id. Returns the kept record.

    places, when given, is every record already loaded (load_places). It is
    used instead of reading places/ again, and updated with what is written.
    """
    root = Path(root)
    now = now or utc_now()
    places = places if places is not None else load_places(root)

    if keep_id == drop_id:
        raise MergeError("keep and drop are the same record")
    for place_id in (keep_id, drop_id):
        if place_id not in places:
            raise MergeError(f"no such place {place_id}")
        if places[place_id].get("status") == "merged":
            raise MergeError(f"{place_id} is already merged into {places[place_id]['merged_into']}")

    if not part and frozenset((keep_id, drop_id)) in load_distinct(root):
        raise MergeError(
            f"{keep_id} and {drop_id} are listed as separate places in moderation/distinct_places.json"
            " (use --part for two parts of one place)"
        )

    dropped = places[drop_id]
    kept = copy.deepcopy(places[keep_id])

    # OSM links. The kept record's own link is not repeated in merged_osm.
    own = {(kept["osm"]["type"], kept["osm"]["id"])} if kept.get("osm") else set()
    links = (osm_links(kept) | osm_links(dropped)) - own
    parts = part_links(kept) | part_links(dropped)
    if part and dropped.get("osm"):
        parts.add((dropped["osm"]["type"], dropped["osm"]["id"]))
    if links:
        kept["merged_osm"] = [
            {"type": t, "id": i, **({"part": True} if (t, i) in parts else {})} for t, i in sorted(links)
        ]

    # Facts from feedback and contributions, where the kept record has none.
    for key, source in sorted(dropped.get("fact_sources", {}).items()):
        if key in FACT_KEYS and source in MOVABLE_SOURCES and key not in kept["facts"]:
            if key in dropped["facts"]:
                kept["facts"][key] = dropped["facts"][key]
                kept["fact_sources"][key] = source
                if key in dropped.get("fact_dates", {}):
                    kept.setdefault("fact_dates", {})[key] = dropped["fact_dates"][key]

    # The newer of the two confirmations.
    confirmed = max(kept.get("last_confirmed", ""), dropped.get("last_confirmed", ""))
    if confirmed:
        kept["last_confirmed"] = confirmed

    # Reviews.
    moved = [dict(r, place=keep_id) for r in read_reviews(root, drop_id)]
    reviews = sorted(read_reviews(root, keep_id) + moved, key=lambda r: r["id"])
    if reviews:
        blocked = load_blocked(root)
        stars = [r["stars"] for r in reviews if is_visible(r, blocked)]
        kept["rating"] = {"count": len(stars), "mean": rating_mean(stars)} if stars else None
        write_text(review_path(root, keep_id), "".join(dump_line(r) + "\n" for r in reviews))
    if moved:
        review_path(root, drop_id).unlink()

    # Fees.
    moved_fees = [dict(f, place=keep_id) for f in read_fees(root, drop_id)]
    if moved_fees:
        # One fee per contributor per site type: ids are ULIDs, so the larger is newer.
        newest: dict[tuple[str, str], dict] = {}
        for fee in sorted(read_fees(root, keep_id) + moved_fees, key=lambda f: f["id"]):
            newest[(fee["contributor"], fee["site"])] = fee
        fees = sorted(newest.values(), key=lambda f: f["id"])
        write_text(fee_path(root, keep_id), "".join(dump_line(f) + "\n" for f in fees))
        fee_path(root, drop_id).unlink()

    kept["updated"] = now
    write_text(place_path(root, keep_id), dump_record(kept))

    tombstone = dict(dropped, status="merged", merged_into=keep_id)
    write_text(place_path(root, drop_id), dump_record(tombstone))

    retargeted = [dict(r, merged_into=keep_id) for r in places.values() if r.get("merged_into") == drop_id]
    for record in retargeted:
        write_text(place_path(root, record["id"]), dump_record(record))

    places[keep_id], places[drop_id] = kept, tombstone
    for record in retargeted:
        places[record["id"]] = record
    return kept


# ------------------------------------------------------- the automatic batch

AUTO_MERGES_FILE = Path("moderation") / "automatic-merges.csv"
AUTO_MERGES_COLUMNS = (
    "merged", "kept_id", "dropped_id", "kept_name", "dropped_name",
    "kept_kind", "dropped_kind", "kept_osm", "dropped_osm", "distance_m", "part",
)


def keep_order(record: dict) -> tuple:
    """Sort key for the record a group keeps: first an OSM link, then a camp
    site over a motel or hostel, then the most facts, then a kind that says
    more than campground, then the oldest id."""
    return (
        record.get("osm") is None, record["kind"] in ROOFED_KINDS, -len(record.get("facts", {})),
        record["kind"] == "campground", record["id"],
    )


def osm_label(record: dict) -> str:
    osm = record.get("osm")
    return f"{osm['type']}/{osm['id']}" if osm else ""


def merge_batch(root: Path, geometry: Geometry | None = None, now: str | None = None, groups=("overnight",)) -> list[dict]:
    """Merge every likely duplicate the automatic merge rule accepts, in the given groups.

    Pairs come from pipeline.duplicates.find_duplicates and must pass
    pipeline.duplicates.automatic_merge. Pairs that share a record form one
    group of records, which is merged into the record keep_order puts first;
    a record that does not pass the rule against that one is left alone. A
    motel or hostel merged with a camp site (ROOFED_KINDS on one side only)
    is merged as a part, so the double mapping challenge leaves it out.
    Each merge is added to moderation/automatic-merges.csv for a person to
    review. Returns the merges made, as {"kept", "dropped", "distance_m"}.
    """
    root = Path(root)
    now = now or utc_now()
    places = load_places(root)
    distinct = load_distinct(root)
    pairs = [
        (pair["a"], pair["b"])
        for pair in find_duplicates(list(places.values()), distinct, geometry)
        if pair["a"]["group"] in groups and automatic_merge(pair["a"], pair["b"], geometry, distinct) is None
    ]

    # Groups of records joined by accepted pairs.
    parent: dict[str, str] = {}

    def root_of(place_id: str) -> str:
        while parent.setdefault(place_id, place_id) != place_id:
            place_id = parent[place_id]
        return place_id

    for a, b in pairs:
        parent[root_of(a["id"])] = root_of(b["id"])
    members: dict[str, list[str]] = {}
    for place_id in parent:
        members.setdefault(root_of(place_id), []).append(place_id)

    merges = []
    for ids in sorted(sorted(group) for group in members.values()):
        ordered = sorted((places[i] for i in ids), key=keep_order)
        keeper = ordered[0]
        for other in sorted(ordered[1:], key=lambda r: r["id"]):
            if automatic_merge(places[keeper["id"]], other, geometry, distinct) is not None:
                continue
            distance = round(distance_m(keeper, other))
            # A motel or hostel and the caravan park it stands in are two
            # features of one place in OSM, drawn apart on purpose.
            part = (keeper["kind"] in ROOFED_KINDS) != (other["kind"] in ROOFED_KINDS)
            row = {
                "merged": now[:10],
                "kept_id": keeper["id"], "dropped_id": other["id"],
                "kept_name": keeper.get("name") or "", "dropped_name": other.get("name") or "",
                "kept_kind": keeper["kind"], "dropped_kind": other["kind"],
                "kept_osm": osm_label(keeper), "dropped_osm": osm_label(other),
                "distance_m": distance, "part": "yes" if part else "no",
            }
            merge(root, keeper["id"], other["id"], now=now, part=part, places=places)
            merges.append({"kept": keeper["id"], "dropped": other["id"], "distance_m": distance, "row": row})
    if merges:
        append_review(root, [m.pop("row") for m in merges])
    return merges


def append_review(root: Path, rows: list[dict]) -> None:
    """Add rows to moderation/automatic-merges.csv, after the rows already there."""
    path = Path(root) / AUTO_MERGES_FILE
    existing = []
    if path.exists():
        with open(path, encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=AUTO_MERGES_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(existing + rows)
    write_text(path, out.getvalue())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("keep_id", nargs="?")
    parser.add_argument("drop_id", nargs="?")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--part", action="store_true", help="the OSM objects are parts of one place, not double mapping")
    parser.add_argument("--auto", action="store_true", help="merge every pair the automatic merge rule accepts")
    parser.add_argument("--geometry", type=Path, help="with --auto: the sidecar pipeline.extract --geometry wrote (needed for the road test)")
    parser.add_argument("--group", action="append", help="with --auto: a group to merge in (default overnight; repeat for more)")
    args = parser.parse_args(argv)
    if args.auto:
        if args.keep_id or args.part:
            parser.error("--auto takes no ids and no --part")
        if args.geometry is None:
            parser.error("--auto needs --geometry: without roads the rule cannot tell opposite sides of a road apart")
        merges = merge_batch(args.root, load_geometry(args.geometry), groups=tuple(args.group or ["overnight"]))
        print(f"Merged {len(merges)} records; each is listed in {AUTO_MERGES_FILE.as_posix()}", file=sys.stderr)
        return 0
    if not args.keep_id or not args.drop_id:
        parser.error("give keep_id and drop_id, or --auto")
    try:
        kept = merge(args.root, args.keep_id, args.drop_id, part=args.part)
    except MergeError as exc:
        print(f"Not merged: {exc}", file=sys.stderr)
        return 1
    print(f"Merged {args.drop_id} into {args.keep_id} ({kept['name'] or 'unnamed'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
