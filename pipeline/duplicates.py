"""Find places that are likely the same place recorded twice.

Usage:
    python -m pipeline.duplicates --root .

The OSM sync runs this after every sync and lists the pairs in its PR body
under "Possible duplicates". It only flags. Merging is done by a person with
pipeline.merge, one pair at a time or in a batch of the pairs the automatic
merge rule accepts (automatic_merge, below).

Two places are a likely duplicate when all of these hold:
- They are comparable: both in the overnight group (a caravan park and a
  paid campground can be the same place), or of the same kind.
- They are within 200 m of each other.
- Their names are similar (a ratio of 0.8 or more once generic words such as
  "campground" or "caravan park" are removed), or one of them has no name and
  they are within 50 m.
Never paired:
- two utility places that both have no name (unnamed toilets and taps often
  stand side by side) or two overnight places that both have no name (often
  separate parts or pitches of one park);
- two places whose names differ only in their numbers, unless the numbers
  are equal ("Site 20" and "Site 26" are separate pitches);
- merged records (tombstones);
- pairs a person checked and listed in moderation/distinct_places.json as
  separate places;
- pairs the rules in pipeline.geometry find to be separate places: a major
  road between them, or different directions, operators or access.

The automatic merge rule (automatic_merge) is narrower: the same name once
tidied, the same group, less than 50 m apart and nothing in pipeline.geometry
that keeps them apart. python -m pipeline.merge --auto applies it to the
pairs found here, and the feedback engine applies the same rule to a new
place a traveller adds next to an existing one.

The distance is between the two records' locations, as before. The outlines
in the geometry sidecar (--geometry, written by pipeline.extract) only keep
together an object and the area it lies in, which no road can split.
Without the sidecar there are no roads to test, and only names and the
records' own facts count as signals.
"""

import argparse
import json
import math
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

from .common import load_distinct, load_places
from .geometry import Geometry, group_of, judge, load_geometry

MAX_DISTANCE_M = 200
UNNAMED_MAX_DISTANCE_M = 50
NAME_RATIO = 0.8

# Words left out when names are compared.
GENERIC_WORDS = {
    "campground", "campgrounds", "camping", "camp", "camps", "area", "reserve",
    "park", "caravan", "holiday", "the",
}

# Grid cell size in degrees. A cell is at least 200 m across in both
# directions everywhere in Australia, including Macquarie Island at 54 S, so
# a pair is always in the same or a neighbouring cell.
CELL_LAT = 0.0025
CELL_LON = 0.004


def normalise_name(name: str | None) -> str | None:
    """Lower case, punctuation removed, generic words left out. None stays None."""
    if name is None:
        return None
    words = re.sub(r"[^0-9a-z]+", " ", name.lower()).split()
    return " ".join(word for word in words if word not in GENERIC_WORDS)


# The automatic merge rule (see automatic_merge).
AUTO_MERGE_MAX_DISTANCE_M = 50
# Words tidy_name also leaves out, and short forms it writes in full.
TIDY_DROPPED = {"and"}
TIDY_EXPANDED = {"ra": "rest area", "ck": "creek", "mt": "mount"}


def tidy_name(name: str | None) -> str:
    """A name tidied for the automatic merge rule, "" when nothing distinctive is left.

    Lower case, punctuation removed, the short forms RA, Ck and Mt written
    in full, then "and" and the generic words normalise_name leaves out
    dropped. "Dalmeny Caravan & Camping Ground" and "Dalmeny Caravan And
    Camping Ground" both tidy to "dalmeny ground", "Calliope River RA" and
    "Calliope River Rest Area" to "calliope river rest".
    """
    if not name:
        return ""
    words = re.sub(r"[^0-9a-z]+", " ", name.lower()).split()
    words = " ".join(TIDY_EXPANDED.get(word, word) for word in words).split()
    return " ".join(word for word in words if word not in GENERIC_WORDS and word not in TIDY_DROPPED)


def distance_m(a: dict, b: dict) -> float:
    """Great-circle distance between two records, in metres."""
    lat1, lon1 = math.radians(a["location"]["lat"]), math.radians(a["location"]["lon"])
    lat2, lon2 = math.radians(b["location"]["lat"]), math.radians(b["location"]["lon"])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


NUMBER = re.compile(r"\d+")


def differ_only_in_numbers(a: str, b: str) -> bool:
    """True when two normalised names are the same apart from different numbers."""
    return NUMBER.sub("#", a) == NUMBER.sub("#", b) and NUMBER.findall(a) != NUMBER.findall(b)


def comparable(a: dict, b: dict) -> bool:
    if a["group"] == "overnight" and b["group"] == "overnight":
        return True
    return a["kind"] == b["kind"]


def likely_duplicate(a: dict, b: dict, distance: float) -> bool:
    if distance > MAX_DISTANCE_M or not comparable(a, b):
        return False
    if a["name"] is None or b["name"] is None:
        if a["name"] is None and b["name"] is None and a["group"] in ("utility", "overnight"):
            return False
        return distance <= UNNAMED_MAX_DISTANCE_M
    name_a, name_b = normalise_name(a["name"]), normalise_name(b["name"])
    if differ_only_in_numbers(name_a, name_b):
        return False
    return SequenceMatcher(None, name_a, name_b).ratio() >= NAME_RATIO


def automatic_merge(
    a: dict, b: dict, geometry: Geometry | None = None, distinct: set[frozenset[str]] = frozenset()
) -> str | None:
    """None when two records may be merged without a person, else why not.

    The automatic merge rule. All of these must hold:
    - neither is merged, and the pair is not on the distinct list;
    - neither has a verified owner (an owner's values do not move in a merge);
    - they are in the same group, and outside the overnight group of the
      same kind, as the duplicate finder compares them (a toilet named for
      its rest area is not the rest area);
    - their names are the same once tidied (tidy_name), and not empty;
    - they are less than AUTO_MERGE_MAX_DISTANCE_M (50 m) apart;
    - the rules in pipeline.geometry do not find them to be separate places:
      no major road between them, and no different direction, operator or
      entry.

    geometry is the loaded sidecar (pipeline.extract --geometry). Without
    it there are no roads to test, so a caller without one must not treat
    None as enough on its own. Every other likely duplicate waits for a
    reviewer.
    """
    geometry = geometry or Geometry()
    if a["id"] == b["id"]:
        return "same record"
    for record in (a, b):
        if record.get("status") == "merged":
            return f"{record['id']} is already merged"
        if record.get("owner_verified"):
            return f"{record['id']} has a verified owner"
    if frozenset((a["id"], b["id"])) in distinct:
        return "on the distinct list"
    if group_of(a) != group_of(b):
        return f"group differs: {group_of(a)} / {group_of(b)}"
    if group_of(a) != "overnight" and a["kind"] != b["kind"]:
        return f"kind differs outside the overnight group: {a['kind']} / {b['kind']}"
    name_a, name_b = tidy_name(a.get("name")), tidy_name(b.get("name"))
    if not name_a or not name_b:
        return "name has nothing distinctive once tidied"
    if name_a != name_b:
        return f"name differs once tidied: {name_a} / {name_b}"
    distance = distance_m(a, b)
    if distance >= AUTO_MERGE_MAX_DISTANCE_M:
        return f"distance {distance:.0f} m is not under {AUTO_MERGE_MAX_DISTANCE_M} m"
    verdict = judge(a, b, geometry, MAX_DISTANCE_M)
    if verdict.separate:
        return f"{verdict.reason}: {verdict.detail}"
    return None


def find_duplicates(
    records: list[dict], distinct: set[frozenset[str]] = frozenset(), geometry: Geometry | None = None
) -> list[dict]:
    """Likely duplicate pairs, as {"a", "b", "distance_m"} with a's id before b's, sorted.

    distinct holds id pairs known to be separate places, which are left out.
    geometry is the loaded sidecar. Without it there are no outlines or roads
    to test, and only the record's own facts count as signals.
    """
    geometry = geometry or Geometry()
    live = [r for r in records if r.get("status") != "merged"]
    grid: dict[tuple[int, int], list[dict]] = {}
    for record in live:
        cell = (
            math.floor(record["location"]["lat"] / CELL_LAT),
            math.floor(record["location"]["lon"] / CELL_LON),
        )
        grid.setdefault(cell, []).append(record)

    pairs = []
    for (row, col), here in grid.items():
        nearby = [
            other
            for d_row in (-1, 0, 1)
            for d_col in (-1, 0, 1)
            for other in grid.get((row + d_row, col + d_col), [])
        ]
        for a in here:
            for b in nearby:
                if a["id"] >= b["id"] or frozenset((a["id"], b["id"])) in distinct:
                    continue
                distance = distance_m(a, b)
                if likely_duplicate(a, b, distance) and not judge(a, b, geometry, MAX_DISTANCE_M).separate:
                    pairs.append({"a": a, "b": b, "distance_m": distance})
    pairs.sort(key=lambda p: (p["a"]["id"], p["b"]["id"]))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--geometry", type=Path, help="geometry sidecar written by pipeline.extract --geometry")
    args = parser.parse_args(argv)
    # One JSON object per line, always UTF-8, whatever the console uses.
    sys.stdout.reconfigure(encoding="utf-8")
    places = list(load_places(args.root).values())
    for pair in find_duplicates(places, load_distinct(args.root), load_geometry(args.geometry)):
        a, b = pair["a"], pair["b"]
        print(json.dumps({
            "a": a["id"], "a_name": a["name"], "a_kind": a["kind"],
            "b": b["id"], "b_name": b["name"], "b_kind": b["kind"],
            "distance_m": round(pair["distance_m"]),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
