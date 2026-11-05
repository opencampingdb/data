"""List rest areas and parking that only their name marks as for heavy vehicles.

Usage:
    python -m pipeline.vehicle_access --root .

The fact vehicle_access is set only from OSM tags (pipeline.extract). Many
truck bays say so only in their name ("Truck Parking Bay", "Heavy Vehicle
Rest Area"), which is not enough to set the fact. This writes them to
moderation/heavy-vehicle-candidates.csv for a reviewer to check, one row per
place: place (its id), name and location ("lat,lon"), sorted by id, quoted
as Python's csv module writes it. The feedback engine reads the place column. A place is a
candidate when it is a rest_area or long_vehicle_parking, is not merged, has
no vehicle_access fact, and its name says truck, heavy vehicle, HV or
B-double.
"""

import argparse
import csv
import io
import re
import sys
from pathlib import Path

from .common import load_places, write_text

CANDIDATES_FILE = Path("moderation") / "heavy-vehicle-candidates.csv"
CANDIDATE_KINDS = ("rest_area", "long_vehicle_parking")
NAME_RE = re.compile(r"\b(trucks?|heavy[\s-]*vehicles?|hv|b[\s-]*doubles?)\b", re.IGNORECASE)


def name_candidates(places: dict[str, dict]) -> list[dict]:
    """The places whose name alone says heavy vehicles only, sorted by id."""
    return [
        places[place_id] for place_id in sorted(places)
        if places[place_id].get("status") != "merged"
        and places[place_id]["kind"] in CANDIDATE_KINDS
        and "vehicle_access" not in places[place_id].get("facts", {})
        and NAME_RE.search(places[place_id].get("name") or "")
    ]


def write_candidates(root: Path) -> int:
    """Write the candidates file and return how many places it lists."""
    candidates = name_candidates(load_places(root))
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["place", "name", "location"])
    for record in candidates:
        location = f"{record['location']['lat']},{record['location']['lon']}"
        writer.writerow([record["id"], record["name"], location])
    write_text(Path(root) / CANDIDATES_FILE, out.getvalue())
    return len(candidates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    args = parser.parse_args(argv)
    count = write_candidates(args.root)
    print(f"{count} candidates in {CANDIDATES_FILE.as_posix()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
