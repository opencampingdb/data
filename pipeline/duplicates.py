"""Find places that are likely the same place recorded twice.

Usage:
    python -m pipeline.duplicates --root .

The OSM sync runs this after every sync and lists the pairs in its PR body
under "Possible duplicates". It only flags. Merging is done by a person with
pipeline.merge.

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
- merged records (tombstones).
"""

import argparse
import json
import math
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path

from .common import load_places

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


def find_duplicates(records: list[dict]) -> list[dict]:
    """Likely duplicate pairs, as {"a", "b", "distance_m"} with a's id before b's, sorted."""
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
                if a["id"] >= b["id"]:
                    continue
                distance = distance_m(a, b)
                if likely_duplicate(a, b, distance):
                    pairs.append({"a": a, "b": b, "distance_m": distance})
    pairs.sort(key=lambda p: (p["a"]["id"], p["b"]["id"]))
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    args = parser.parse_args(argv)
    # One JSON object per line, always UTF-8, whatever the console uses.
    sys.stdout.reconfigure(encoding="utf-8")
    for pair in find_duplicates(list(load_places(args.root).values())):
        a, b = pair["a"], pair["b"]
        print(json.dumps({
            "a": a["id"], "a_name": a["name"], "a_kind": a["kind"],
            "b": b["id"], "b_name": b["name"], "b_kind": b["kind"],
            "distance_m": round(pair["distance_m"]),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
