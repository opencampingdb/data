"""Write the MapRoulette challenge files from places/ and an OSM extract.

Usage:
    python -m pipeline.maproulette extract.ndjson --root . [--report sync-report.md]

--report appends a section to the sync report naming the tasks each file
gains and loses, so the person approving the sync PR sees them.

Writes two files in maproulette/, as line-by-line GeoJSON (RFC 7464: each
line is a record separator and one FeatureCollection, and each line becomes
one task). MapRoulette builds and rebuilds the challenges from these files on
main, so a task appears only after the change behind it was approved there.

maproulette/double_mapping.geojson
    One task per place that OSM maps more than once. A person merged the
    records with pipeline.merge, so the kept record's merged_osm lists the
    other objects. A task is written while the record's own object and at
    least one merged object are both still in OSM. Objects marked
    "part": true (drawn apart on purpose) do not count.

maproulette/traveller_reports.geojson
    One task per place where a value from feedback or the owner still
    differs from OSM: facts that have an OSM tag, the name, and a permanent
    closure. A task goes away by itself once OSM shows the same value. The
    rules follow the feedback engine's OSM notes (docs/osm.md there):
    contributed values and places without an OSM link are never sent, nor
    booking details, nor a text value holding an email address, phone number
    or link (the contact facts aside).

Values from community reports (source community_report) never reach either
file, and neither does a place whose name or location came from them, even
if it has an OSM link: those reports are kept out of OSM.

Each feature carries "id" (the Open Camping DB id, which MapRoulette uses to
match a task across rebuilds) and "@id" (the OSM object, such as node/123).
Lines are sorted by id, so the same input always gives the same files.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from .common import from_community_report, load_places, write_text
from .sync import load_extract

DATA_REPO_URL = "https://github.com/opencampingdb/data"
RS = "\x1e"

DOUBLE_MAPPING = "double_mapping.geojson"
TRAVELLER_REPORTS = "traveller_reports.geojson"

# Only these sources are passed to OSM's mappers. Everything in NOT_FOR_OSM
# (contributed values, community reports) stays in Open Camping DB.
REPORT_SOURCES = ("feedback", "owner")
NEVER_SENT = ("booking_url", "booking_required")
CONTACT_FACTS = ("website", "phone", "phone_2", "email", "facebook", "instagram", "tiktok", "x")
CONTACT_RES = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<![\w])\+?\d(?:[\s().-]{0,2}\d){7,}"),
    re.compile(r"(?i)https?://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|au|io|me)\b"),
)

# Fact -> OSM key for facts read from a plain yes/no tag (pipeline.extract).
YES_NO_TAGS = {
    "fee": "fee",
    "power": "power_supply",
    "tents": "tents",
    "caravans": "caravans",
    "toilets": "toilets",
    "showers": "shower",
    "drinking_water": "drinking_water",
    "dump_station": "sanitary_dump_station",
    "wheelchair": "wheelchair",
    "motorhomes": "motorhome",
    "laundry": "washing_machine",
    "kitchen": "kitchen",
    "bbq": "bbq",
    "picnic_tables": "picnic_table",
    "campfires": "openfire",
    "cabins": "cabins",
    "playground": "playground",
    "nudist": "nudism",
}
# Fact -> OSM key for facts copied from a tag as written.
WRITTEN_TAGS = {
    "max_stay": "maxstay",
    "opening_hours": "opening_hours",
    "website": "website",
    "phone": "phone",
    "email": "email",
    "facebook": "contact:facebook",
    "instagram": "contact:instagram",
    "tiktok": "contact:tiktok",
    "x": "contact:twitter",
}
# Fact -> value -> OSM tag, for facts with a fixed set of values.
VALUE_TAGS = {
    "fee": {"donation": "fee=donation"},
    "pets": {"yes": "dog=yes", "leashed": "dog=leashed", "no": "dog=no"},
    "access": {"2wd": "4wd_only=no", "awd": "4wd_only=recommended", "4wd": "4wd_only=yes"},
    "entry": {"public": "access=yes", "customers": "access=customers", "permit": "access=permit", "private": "access=private"},
    "water_type": {"tank_fill": "amenity=water_point", "fountain": "amenity=drinking_water"},
    "brand": {
        "big4": "brand=BIG4",
        "discovery": "brand=Discovery Parks",
        "nrma": "brand=NRMA Parks",
        "ingenia": "brand=Ingenia Holidays",
        "reflections": "brand=Reflections Holiday Parks",
        "gday": "brand=G'day Parks",
        "family_parks": "brand=Family Parks",
        "yha": "brand=YHA",
    },
}

# Plain words for a yes/no fact: (when true, when false).
YES_NO_WORDS = {
    "fee": ("there is a fee to stay", "there is no fee to stay"),
    "power": ("there are powered sites", "there are no powered sites"),
    "tents": ("tents are allowed", "tents are not allowed"),
    "caravans": ("caravans are allowed", "caravans are not allowed"),
    "toilets": ("there are toilets", "there are no toilets"),
    "showers": ("there are showers", "there are no showers"),
    "drinking_water": ("there is drinking water", "there is no drinking water"),
    "dump_station": ("there is a dump point", "there is no dump point"),
    "wheelchair": ("it is wheelchair accessible", "it is not wheelchair accessible"),
    "motorhomes": ("motorhomes can get in", "motorhomes cannot get in"),
    "laundry": ("there is a laundry", "there is no laundry"),
    "kitchen": ("there is a camp kitchen", "there is no camp kitchen"),
    "bbq": ("there are barbecues", "there are no barbecues"),
    "picnic_tables": ("there are tables", "there are no tables"),
    "campfires": ("campfires are allowed", "campfires are not allowed"),
    "cabins": ("there are cabins", "there are no cabins"),
    "playground": ("there is a playground", "there is no playground"),
    "wifi": ("there is Wi-Fi", "there is no Wi-Fi"),
    "nudist": ("it is a nudist place", "it is not a nudist place"),
}
VALUE_WORDS = {
    "fee": {"donation": "payment is by donation"},
    "pets": {"yes": "dogs are allowed", "leashed": "dogs are allowed on a lead", "no": "dogs are not allowed"},
    "access": {"2wd": "the road is fine for 2WD", "awd": "AWD is recommended", "4wd": "the road is 4WD only"},
    "entry": {
        "public": "anyone may stay",
        "customers": "only customers may stay",
        "permit": "a permit is needed to stay",
        "private": "it is private, not open to the public",
    },
    "water_type": {"tank_fill": "it is a tap for filling tanks", "fountain": "it is a drinking fountain"},
}
LABELS = {
    "max_stay": "maximum stay",
    "opening_hours": "opening hours",
    "website": "website",
    "phone": "phone number",
    "email": "email address",
    "facebook": "Facebook page",
    "instagram": "Instagram page",
    "tiktok": "TikTok page",
    "x": "X page",
    "brand": "brand",
    "name": "name",
}
WHO = {"feedback": "Travellers report", "owner": "The owner reports"}


def osm_tag(field: str, value) -> str | None:
    """key=value for a fact, or None when no OSM tag matches the value."""
    if field == "wifi" and isinstance(value, bool):
        return f"internet_access={'wlan' if value else 'no'}"
    if isinstance(value, bool):
        key = YES_NO_TAGS.get(field)
        return f"{key}={'yes' if value else 'no'}" if key else None
    if not isinstance(value, str) or not value.strip():
        return None
    if field in WRITTEN_TAGS:
        return f"{WRITTEN_TAGS[field]}={value}"
    return VALUE_TAGS.get(field, {}).get(value)


def describe(field: str, value) -> str:
    """Plain words for a field and its value."""
    if isinstance(value, bool) and field in YES_NO_WORDS:
        return YES_NO_WORDS[field][0 if value else 1]
    fixed = VALUE_WORDS.get(field, {}).get(value)
    if fixed:
        return fixed
    if field == "brand":
        return f"it is a {osm_tag(field, value).split('=', 1)[1]} park"
    return f"the {LABELS.get(field, field.replace('_', ' '))} is {value}"


def sendable(field: str, value) -> bool:
    """False for a text value holding an email address, phone number or link."""
    if not isinstance(value, str) or field in CONTACT_FACTS:
        return True
    return not any(pattern.search(value) for pattern in CONTACT_RES)


def osm_ref(link: dict) -> str:
    return f"{link['type']}/{link['id']}"


def record_url(record: dict) -> str:
    return f"{DATA_REPO_URL}/blob/main/places/{record['id'][-2:].lower()}/{record['id']}.json"


def feature(record: dict, ref: str, location: dict, properties: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [location["lon"], location["lat"]]},
        "properties": {"id": record["id"], "@id": ref, **properties},
    }


def double_mapping_tasks(places: dict, extract: dict) -> list[dict]:
    tasks = []
    for record in places.values():
        if record.get("status") == "merged" or not record.get("osm") or not record.get("merged_osm"):
            continue
        if from_community_report(record):
            continue
        own = osm_ref(record["osm"])
        refs = [own] + [osm_ref(m) for m in record["merged_osm"] if not m.get("part")]
        present = [ref for ref in refs if ref in extract]
        if own not in present or len(present) < 2:
            continue
        objects = " and ".join(present)
        properties = {
            "name": record.get("name") or "Unnamed place",
            "objects": objects,
            "record": record_url(record),
        }
        tasks.append({
            "type": "FeatureCollection",
            "features": [feature(record, ref, extract[ref]["location"], properties) for ref in present],
        })
    return tasks


def report_changes(record: dict, osm_place: dict) -> list[tuple[str, str]]:
    """(source, line) for each value from feedback or the owner that OSM does not show yet."""
    sources = record.get("fact_sources", {})
    osm_facts = osm_place.get("facts", {})
    changes = []
    for field in sorted(record.get("facts", {})):
        value = record["facts"][field]
        source = sources.get(field)
        if source not in REPORT_SOURCES or field in NEVER_SENT or osm_facts.get(field) == value:
            continue
        if field == "access" and value == "unknown":
            continue
        tag = osm_tag(field, value)
        if tag and sendable(field, value):
            changes.append((source, f"{describe(field, value)} ({tag})"))
    name = record.get("name")
    if name and name != osm_place.get("name") and sources.get("name") in REPORT_SOURCES and sendable("name", name):
        changes.append((sources["name"], f"the name is {name} (name={name})"))
    if record.get("status") == "closed" and sources.get("status") in REPORT_SOURCES:
        changes.append((sources["status"], "it has closed for good. No single tag says this, so please check and edit as you see fit"))
    return changes


def traveller_report_tasks(places: dict, extract: dict) -> list[dict]:
    tasks = []
    for record in places.values():
        if record.get("status") == "merged" or not record.get("osm") or from_community_report(record):
            continue
        ref = osm_ref(record["osm"])
        if ref not in extract:
            continue
        changes = report_changes(record, extract[ref])
        if not changes:
            continue
        lines = [f"- {WHO[source]} {text}." for source, text in changes]
        properties = {
            "name": record.get("name") or "Unnamed place",
            "changes": "\n".join(lines),
            "record": record_url(record),
        }
        tasks.append({"type": "FeatureCollection", "features": [feature(record, ref, record["location"], properties)]})
    return tasks


def geojson_lines(tasks: list[dict]) -> str:
    tasks = sorted(tasks, key=lambda t: t["features"][0]["properties"]["id"])
    return "".join(RS + json.dumps(t, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for t in tasks)


def task_names(path: Path) -> dict[str, str]:
    """Task id -> place name for a challenge file, empty when it does not exist."""
    if not path.exists():
        return {}
    names = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.strip(RS).strip():
            properties = json.loads(line.lstrip(RS))["features"][0]["properties"]
            names[properties["id"]] = properties["name"]
    return names


def write_challenges(root: Path, extract: list[dict]) -> dict[str, dict]:
    """Write both challenge files. Returns, per file, the task count and the ids added and removed."""
    root = Path(root)
    places = load_places(root)
    by_ref = {osm_ref(place["osm"]): place for place in extract}
    files = {
        DOUBLE_MAPPING: double_mapping_tasks(places, by_ref),
        TRAVELLER_REPORTS: traveller_report_tasks(places, by_ref),
    }
    result = {}
    for name, tasks in files.items():
        path = root / "maproulette" / name
        before = task_names(path)
        write_text(path, geojson_lines(tasks))
        after = task_names(path)
        result[name] = {
            "count": len(after),
            "added": sorted((i, after[i]) for i in after.keys() - before.keys()),
            "removed": sorted((i, before[i]) for i in before.keys() - after.keys()),
        }
    return result


TITLES = {DOUBLE_MAPPING: "Double mapping", TRAVELLER_REPORTS: "Traveller reports"}


def render_report(result: dict[str, dict]) -> str:
    lines = ["", "## MapRoulette", ""]
    for name, change in result.items():
        lines.append(
            f"- {TITLES[name]} (`maproulette/{name}`): {change['count']} task(s), "
            f"{len(change['added'])} new, {len(change['removed'])} gone."
        )
        for word, items in (("New", change["added"]), ("Gone", change["removed"])):
            for place_id, place_name in items:
                lines.append(f"  - {word}: {place_name} ({place_id})")
    lines += ["", "Merging this PR approves these tasks. Then rebuild the challenges in MapRoulette (see maproulette/README.md).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("extract", type=Path, help="NDJSON written by pipeline.extract")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--report", type=Path, help="sync report to append the MapRoulette section to")
    args = parser.parse_args(argv)
    result = write_challenges(args.root, load_extract(args.extract))
    for name, change in result.items():
        print(f"{name}: {change['count']} task(s), {len(change['added'])} new, {len(change['removed'])} gone", file=sys.stderr)
    if args.report:
        with open(args.report, "a", encoding="utf-8", newline="\n") as f:
            f.write(render_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
