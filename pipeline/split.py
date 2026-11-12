"""Split merged records whose OSM objects are separate places. Run by a person.

Usage:
    python -m pipeline.split extract.ndjson --geometry geometry.ndjson --root .

For every record that holds more than one OSM object (its own osm link and
merged_osm, leaving out links marked "part": true, which a person said are
one place drawn apart on purpose), the rules in pipeline.geometry are applied to each pair of its
objects as OSM has them now. Rule 1 (one lies in or at the edge of the
other's area) keeps them together. Rule 2 (a major road between them) and
rule 3 (different names, directions, operators or access) split them.

What a split does:
- The record keeps its id, its own OSM object and every object that stays
  with it. Its reviews and the facts it took from feedback stay too.
- Each object that leaves becomes its own place again. When the merge left a
  tombstone for that object, the tombstone comes back to life with its old
  id (status "open", merged_into removed), so apps that stored that id find
  it again. Otherwise the object gets a new record and a new id, as the sync
  would make one. Objects judged to be one place go together, with the first
  as the record's own object and the rest in merged_osm.
- Older tombstones for the objects that left point at their new record.
- Each new pair goes on moderation/distinct_places.json (reason "direction"
  for a road or a direction, "other" otherwise), so the duplicate finder and
  pipeline.merge leave it alone from then on.
- Each split is added to moderation/opposite-side-splits.csv for a person to
  review: the old id, the new id, both names, the OSM objects each keeps,
  the distance between them and the road crossed.

Objects missing from the extract are left where they are, since there is
nothing to judge them by. Running it again splits nothing new.
"""

import argparse
import copy
import csv
import io
import json
import sys
from datetime import date
from pathlib import Path

from .common import (
    distinct_path,
    dump_record,
    load_places,
    place_path,
    utc_now,
    write_text,
)
from .duplicates import MAX_DISTANCE_M
from .geometry import Geometry, judge, load_geometry
from .sync import load_extract, new_place_id, new_record

REVIEW_FILE = "opposite-side-splits.csv"
REVIEW_COLUMNS = (
    "old_id", "new_id", "old_name", "new_name", "old_osm", "new_osm", "distance_m", "reason", "road",
)
NOTE_MAX = 300


def review_path(root: Path) -> Path:
    return Path(root) / "moderation" / REVIEW_FILE


def ref_of(link: dict) -> str:
    return f"{link['type']}/{link['id']}"


def link_of(ref: str) -> tuple[str, int]:
    osm_type, osm_id = ref.split("/")
    return osm_type, int(osm_id)


def group_objects(refs: list[str], extract: dict, geometry: Geometry) -> list[dict]:
    """Groups of one place each. The first group holds refs[0].

    An object joins the first group where none of the members the extract
    knows is a separate place from it. Each group also notes the verdict
    that first kept it apart from the first group.
    """
    groups: list[dict] = [{"refs": [refs[0]], "verdict": None}]
    for ref in refs[1:]:
        if ref not in extract:
            groups[0]["refs"].append(ref)
            continue
        first_verdict = None
        for group in groups:
            verdicts = [
                judge(extract[member], extract[ref], geometry, MAX_DISTANCE_M)
                for member in group["refs"]
                if member in extract
            ]
            apart = [v for v in verdicts if v.separate]
            if not apart:
                group["refs"].append(ref)
                break
            if first_verdict is None:
                first_verdict = apart[0]
        else:
            groups.append({"refs": [ref], "verdict": first_verdict})
    return groups


def distinct_note(verdict) -> tuple[str, str]:
    """(reason, note) for the distinct list."""
    distance = round(verdict.gap_m)
    if verdict.reason == "road":
        road = verdict.road
        name = road.name or "an unnamed road"
        if road.ref:
            name += f" ({road.ref})"
        reason, text = "direction", f"On opposite sides of {name}, {distance} m apart."
    elif verdict.reason == "bound":
        reason, text = "direction", f"One for each direction of travel, {distance} m apart: {verdict.detail}."
    else:
        reason, text = "other", f"{verdict.detail[0].upper()}{verdict.detail[1:]}, {distance} m apart."
    note = f"{text} Split from one record by pipeline.split."
    return reason, note[:NOTE_MAX]


def split_all(root: Path, extract_places: list[dict], geometry: Geometry, now: str | None = None,
              today: str | None = None, make_id=new_place_id) -> list[dict]:
    """Split every record whose objects the rules find to be separate places.

    Returns one row per new place, as written to the review file.
    """
    root = Path(root)
    now = now or utc_now()
    today = today or date.today().isoformat()
    places = load_places(root)
    extract = {ref_of(p["osm"]): p for p in extract_places}
    tombstones = {
        (ref_of(r["osm"]), r["merged_into"]): r
        for r in places.values()
        if r.get("status") == "merged" and r.get("osm")
    }
    rows: list[dict] = []
    new_pairs: list[dict] = []

    for record in sorted(places.values(), key=lambda r: r["id"]):
        if record.get("status") == "merged" or not record.get("osm") or not record.get("merged_osm"):
            continue
        own = ref_of(record["osm"])
        if own not in extract:
            continue
        # Parts were marked by a person as one place drawn apart on purpose: they stay.
        refs = [own] + [ref_of(m) for m in record["merged_osm"] if not m.get("part")]
        groups = group_objects(refs, extract, geometry)
        if len(groups) == 1:
            continue

        kept = copy.deepcopy(record)
        leaving = {ref for group in groups[1:] for ref in group["refs"]}
        kept["merged_osm"] = [m for m in record["merged_osm"] if ref_of(m) not in leaving]
        if not kept["merged_osm"]:
            del kept["merged_osm"]
        kept["updated"] = now
        write_text(place_path(root, kept["id"]), dump_record(kept))

        made: list[dict] = []
        for group in groups[1:]:
            first, rest = group["refs"][0], group["refs"][1:]
            tombstone = tombstones.get((first, record["id"]))
            if tombstone is not None:
                fresh = {k: v for k, v in tombstone.items() if k != "merged_into"}
                fresh["status"] = "open"
                fresh["updated"] = now
            else:
                fresh = new_record(extract[first], now, make_id)
            fresh.pop("merged_osm", None)
            if rest:
                fresh["merged_osm"] = [
                    {"type": t, "id": i} for t, i in sorted(link_of(ref) for ref in rest)
                ]
            write_text(place_path(root, fresh["id"]), dump_record(fresh))
            for ref in rest:
                old = tombstones.get((ref, record["id"]))
                if old is not None:
                    write_text(place_path(root, old["id"]), dump_record(dict(old, merged_into=fresh["id"])))
            made.append(fresh)

            verdict = group["verdict"]
            road = verdict.road.label() if verdict.road else ""
            rows.append({
                "old_id": record["id"],
                "new_id": fresh["id"],
                "old_name": record.get("name") or "",
                "new_name": fresh.get("name") or "",
                "old_osm": " ".join(groups[0]["refs"]),
                "new_osm": " ".join(group["refs"]),
                "distance_m": str(round(verdict.gap_m)),
                "reason": verdict.detail if verdict.reason != "road" else "major road between",
                "road": road,
            })
            reason, note = distinct_note(verdict)
            new_pairs.append({"ids": (record["id"], fresh["id"]), "reason": reason, "note": note})

        # Places split from one record are separate from each other too.
        for i, a in enumerate(made):
            for b in made[i + 1:]:
                new_pairs.append({
                    "ids": (a["id"], b["id"]),
                    "reason": new_pairs[-1]["reason"],
                    "note": f"Both split from {record['id']} by pipeline.split.",
                })

    if new_pairs:
        add_distinct(root, new_pairs, today)
    if rows:
        add_review_rows(root, rows)
    return rows


def add_distinct(root: Path, new_pairs: list[dict], today: str) -> None:
    path = distinct_path(root)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pairs": []}
    have = {(p["a"], p["b"]) for p in data["pairs"]}
    for pair in new_pairs:
        a, b = sorted(pair["ids"])
        if (a, b) in have:
            continue
        have.add((a, b))
        data["pairs"].append({"a": a, "added": today, "b": b, "note": pair["note"], "reason": pair["reason"]})
    data["pairs"].sort(key=lambda p: (p["a"], p["b"]))
    write_text(path, dump_record(data))


def add_review_rows(root: Path, rows: list[dict]) -> None:
    path = review_path(root)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=REVIEW_COLUMNS, lineterminator="\n")
    if not text:
        writer.writeheader()
    writer.writerows(rows)
    write_text(path, text + out.getvalue())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("extract", type=Path, help="NDJSON written by pipeline.extract")
    parser.add_argument("--geometry", type=Path, required=True, help="geometry sidecar written by pipeline.extract --geometry")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    args = parser.parse_args(argv)
    rows = split_all(args.root, load_extract(args.extract), load_geometry(args.geometry))
    for row in rows:
        print(f"Split {row['new_id']} ({row['new_name'] or 'unnamed'}) from {row['old_id']}: {row['reason']} {row['road']}".rstrip(), file=sys.stderr)
    print(f"{len(rows)} place(s) split off", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
