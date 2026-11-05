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
    "pets_months",
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
    "booking_url",
    "booking_required",
    "wheelchair",
    "generators",
    "motorhomes",
    "camper_trailers",
    "motorbikes",
    "big_rigs",
    "non_drinking_water",
    "laundry",
    "kitchen",
    "bbq",
    "drive_through",
    "ensuite_sites",
    "bins",
    "picnic_tables",
    "campfires",
    "cabins",
    "pub",
    "swimming",
    "fishing",
    "walking_tracks",
    "scenery",
    "wildlife",
    "playground",
    "games_room",
    "shade",
    "cycling_tracks",
    "offroad_tracks",
    "boat_ramp_nearby",
    "near_road",
    "telstra_signal",
    "mobile_signal",
    "wifi",
    "tv_reception",
    "pet_sitting",
    "fuel",
    "lpg",
    "car_wash",
    "vehicle_storage",
    "self_contained_only",
    "crocodiles",
    "seasonal_closure",
    "dangerous_when_wet",
    "over_50s",
    "nudist",
    "brand",
    "email",
    "phone_2",
    "facebook",
    "instagram",
    "tiktok",
    "x",
    "vehicle_access",
)

# Keys fact_sources may hold besides the facts. A missing name, location or
# kind entry means the value comes from OSM. status and description never
# come from OSM, so their entries say who set them.
EXTRA_SOURCE_KEYS = ("name", "location", "kind", "status", "description")

# Where a value came from: OpenStreetMap, app feedback, a place or closure
# contributed directly to Open Camping DB, the place's verified owner, or
# anonymous community reports from public camping groups, imported in bulk.
SOURCES = ("osm", "feedback", "contributed", "owner", "community_report")

# Kinds that are buildings to stay in rather than camp sites. The double
# mapping challenge leaves them out, and an automatic merge of one with a
# camp site marks it as a part of that place.
ROOFED_KINDS = ("roofed_accommodation", "backpacker_hostel")

# Sources whose values never go back to OSM, in any form: not as a
# MapRoulette task, not as an OSM note.
NOT_FOR_OSM = ("contributed", "community_report", "rule")

# The source of a fact the release sets by a documented rule
# (schema/fact_rules.json). It is never stored in places/: only released
# records and the place index carry it, with the rule's name in fact_rules.
RULE = "rule"

# The rules, in file order: {"rule", "kind", "fact", "value"}.
FACT_RULES = tuple(json.loads((SCHEMA_DIR / "fact_rules.json").read_text(encoding="utf-8"))["rules"])
RULE_NAMES = tuple(rule["rule"] for rule in FACT_RULES)


def with_rule_facts(record: dict) -> dict:
    """The record as released: each rule for its kind sets its fact where the record has no value.

    Returns a new record with the fact, the source rule in fact_sources and the
    rule's name in fact_rules. A value from any source wins over a rule, and a
    rule fact has no fact_dates entry. The record passed in is not changed.
    """
    rules = [rule for rule in FACT_RULES if rule["kind"] == record.get("kind") and rule["fact"] not in record.get("facts", {})]
    if not rules:
        return record
    released = dict(record)
    released["facts"] = dict(record.get("facts", {}))
    released["fact_sources"] = dict(record.get("fact_sources", {}))
    released["fact_rules"] = dict(record.get("fact_rules", {}))
    for rule in rules:
        released["facts"][rule["fact"]] = rule["value"]
        released["fact_sources"][rule["fact"]] = RULE
        released["fact_rules"][rule["fact"]] = rule["rule"]
    return released

# The source of values imported from anonymous community reports. A place
# whose name or location has this source is never passed to OSM at all.
COMMUNITY_REPORT = "community_report"


def from_community_report(record: dict) -> bool:
    """True for a place whose name or location came from community reports."""
    sources = record.get("fact_sources", {})
    return COMMUNITY_REPORT in (sources.get("name"), sources.get("location"))

# pets_months: the months dogs are allowed at a seasonal place, in OSM's month
# range syntax, such as "May-Sep", "Dec-Feb" (wrapping the year end) or
# "Apr-May,Sep-Oct". It converts to and from dog:conditional=yes @ (May-Sep).
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
PETS_MONTHS_PATTERN = r"^{m}(-{m})?(,{m}(-{m})?)*$".format(m="(" + "|".join(MONTHS) + ")")

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


def fee_path(root: Path, place_id: str) -> Path:
    """Where the fees reported for one place live: fees/<place id>.ndjson."""
    return Path(root) / "fees" / f"{place_id}.ndjson"


def dump_record(record: dict) -> str:
    """Canonical text of a place record file.

    Sorted keys, two-space indent, UTF-8 characters left as they are, and a
    trailing newline. The same record always gives the same text.
    """
    return json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def dump_line(obj: dict) -> str:
    """Canonical one-line JSON (review and fee lines, NDJSON output), without the newline.

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


def iter_fee_files(root: Path):
    """Every fee file, in sorted path order."""
    return sorted((Path(root) / "fees").glob("*.ndjson"))


def blocked_path(root: Path) -> Path:
    """The list of blocked contributors: moderation/blocked_contributors.json."""
    return Path(root) / "moderation" / "blocked_contributors.json"


def load_blocked(root: Path) -> set[str]:
    """Contributor ids on the blocked list. Empty when the file is missing."""
    path = blocked_path(root)
    if not path.exists():
        return set()
    entries = json.loads(path.read_text(encoding="utf-8")).get("contributors", [])
    return {entry["contributor"] for entry in entries if isinstance(entry, dict) and "contributor" in entry}


def distinct_path(root: Path) -> Path:
    """Pairs a person checked and found to be separate places: moderation/distinct_places.json."""
    return Path(root) / "moderation" / "distinct_places.json"


def load_distinct(root: Path) -> set[frozenset[str]]:
    """Place id pairs on the distinct list. Empty when the file is missing."""
    path = distinct_path(root)
    if not path.exists():
        return set()
    entries = json.loads(path.read_text(encoding="utf-8")).get("pairs", [])
    return {
        frozenset((entry["a"], entry["b"]))
        for entry in entries
        if isinstance(entry, dict) and "a" in entry and "b" in entry
    }


def is_visible(review: dict, blocked: set[str]) -> bool:
    """Whether a review is published and counts in the rating.

    A hidden review, or one from a blocked contributor, stays in the
    repository but is left out of the release and the rating.
    """
    return "hidden" not in review and review.get("contributor") not in blocked


def load_places(root: Path) -> dict[str, dict]:
    """All place records keyed by id."""
    places = {}
    for path in iter_place_files(root):
        record = json.loads(path.read_text(encoding="utf-8"))
        places[record["id"]] = record
    return places
