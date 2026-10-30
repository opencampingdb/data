"""Constants and helpers shared by every pipeline step.

Everything that decides how a file looks on disk lives here, so that sync,
release and validate all agree on one canonical form.
"""

import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

# Place kinds and their groups, from schema/kinds.json: the OTWD site_types
# vocabulary plus the Open Camping DB extensions, in file order.
_VOCABULARY = json.loads((SCHEMA_DIR / "kinds.json").read_text(encoding="utf-8"))
GROUPS = tuple(_VOCABULARY["groups"])
KINDS = tuple(entry["code"] for entry in _VOCABULARY["kinds"])
KIND_GROUP = {entry["code"]: entry["group"] for entry in _VOCABULARY["kinds"]}
KIND_LABEL = {entry["code"]: entry["label"] for entry in _VOCABULARY["kinds"]}

# Every fact key a place record may carry.
FACT_KEYS = (
    "fee",
    "power",
    "pets",
    "tents",
    "caravans",
    "toilets",
    "showers",
    "drinking_water",
    "dump_station",
    "max_stay",
    "opening_hours",
    "website",
    "phone",
    "access",
    "entry",
    "water_type",
)

# Keys fact_sources may hold besides the facts. A missing entry means the
# value comes from OSM.
EXTRA_SOURCE_KEYS = ("name", "location")

ATTRIBUTION = "© OpenStreetMap contributors, Open Camping DB"
LICENCE = "ODbL-1.0"

# Coordinates are stored with this many decimal places (about 11 cm).
COORD_PLACES = 6


def utc_now() -> str:
    """Current time as an ISO 8601 UTC string with whole seconds."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_coord(value: float) -> float:
    """Round a latitude or longitude to the stored precision."""
    return round(value, COORD_PLACES)


def place_path(root: Path, place_id: str) -> Path:
    """Where a place record lives: places/<last 2 characters of id, lower case>/<id>.json.

    The end of a ULID is random, so records spread evenly over 1,024 folders.
    """
    return Path(root) / "places" / place_id[-2:].lower() / f"{place_id}.json"


def review_path(root: Path, place_id: str) -> Path:
    """Where the reviews for one place live: reviews/<place id>.ndjson (SPEC)."""
    return Path(root) / "reviews" / f"{place_id}.ndjson"


def dump_record(record: dict) -> str:
    """Canonical text of a place record file.

    Sorted keys, two-space indent, UTF-8 characters left as they are, and a
    trailing newline. The same record always gives the same text.
    """
    return json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def dump_line(obj: dict) -> str:
    """Canonical one-line JSON (review lines, NDJSON output), without the newline.

    Compact, sorted keys, UTF-8 characters as they are.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rating_mean(stars: list[int]) -> float:
    """Mean of star ratings rounded to 1 decimal place, half up (4.25 gives 4.3)."""
    mean = Decimal(sum(stars)) / Decimal(len(stars))
    return float(mean.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def write_text(path: Path, text: str) -> None:
    """Write UTF-8 text with LF line endings on every platform."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def iter_place_files(root: Path):
    """Every place record file, in sorted path order."""
    return sorted((Path(root) / "places").glob("**/*.json"))


def iter_review_files(root: Path):
    """Every review file, in sorted path order."""
    return sorted((Path(root) / "reviews").glob("*.ndjson"))


def load_places(root: Path) -> dict[str, dict]:
    """All place records keyed by id."""
    places = {}
    for path in iter_place_files(root):
        record = json.loads(path.read_text(encoding="utf-8"))
        places[record["id"]] = record
    return places
