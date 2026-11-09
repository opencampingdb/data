"""Write the MapRoulette challenge files from places/ and an OSM extract.

Usage:
    python -m pipeline.maproulette extract.ndjson --root . --inbox <inbox checkout> [--report sync-report.md]

--report appends a section to the sync report naming the tasks each file
gains and loses, so the person approving the sync PR sees them.

--inbox is a checkout of the feedback inbox with its applied/ folder. Report
tasks are built only from the evidence there. Without it (a local run, a
fork), the report file is written with no tasks, so no claim goes out without
the reports behind it.

Writes two files in maproulette/, as line-by-line GeoJSON (RFC 7464: each
line is a record separator and one FeatureCollection, and each line becomes
one task). MapRoulette builds and rebuilds the challenges from these files on
main, so a task appears only after the change behind it was approved there.

maproulette/double_mapping.geojson
    One task per place that OSM maps more than once. A person merged the
    records with pipeline.merge, so the kept record's merged_osm lists the
    other objects. A task is written while the record's own object and at
    least one merged object are both still in OSM. Objects marked
    "part": true (drawn apart on purpose) do not count, and neither do
    objects the rules in pipeline.geometry find to be a separate place (a
    major road between them, or different directions, operators or access).
    Motels, cabins and hostels (NOT_CAMP_SITES) are never tasks: the
    challenge is about camp sites.
    With --geometry, the sidecar pipeline.extract wrote, roads and outlines
    are tested too.

maproulette/traveller_reports.geojson
    One task per place where a value from feedback or the owner still
    differs from OSM: facts that have an OSM tag, the name, and a permanent
    closure. A task goes away by itself once OSM shows the same value. The
    rules follow the feedback engine's OSM notes (docs/osm.md there):
    contributed values and places without an OSM link are never sent, nor
    booking details, nor a text value holding an email address, phone number
    or link (the contact facts aside), nor travellers' opening hours with a
    quoted comment (free text no person has read).

    A value is sent only with enough evidence in the inbox's applied items:
    a feedback value needs at least 2 independent sources (distinct origin,
    else device) that reported that same value for the place in the last 90
    days, and those confirming sources must outnumber the sources that
    doubted the fact in the same 90 days by at least 2. A doubt counts
    whatever was decided about it, unless it was refused. An owner value
    needs the verified owner's applied update, and doubts do not change it. A
    name that only describes the place ("Heavy Vehicle Rest Area") is never
    sent. Each line says how many travellers reported the value and when, or
    when the owner set it, and how a mapper can check it. Only counts and
    dates leave the inbox: never a device, an origin or a traveller's words.

Values from community reports (source community_report) never reach either
file, and neither does a place whose name or location came from them, even
if it has an OSM link: those reports are kept out of OSM.

Each feature carries "id" (the Open Camping DB id, which MapRoulette uses to
match a task across rebuilds) and "@id" (the OSM object, such as node/123).
Report tasks repeat the id as "ocdb_id" for the instructions to show.
Lines are sorted by id, so the same input always gives the same files.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import KIND_LABEL, ROOFED_KINDS, from_community_report, load_places, write_text
from .duplicates import MAX_DISTANCE_M
from .geometry import Geometry, judge, load_geometry
from .sync import escape_cell, load_extract

DATA_REPO_URL = "https://github.com/opencampingdb/data"
RS = "\x1e"

DOUBLE_MAPPING = "double_mapping.geojson"
# The double mapping challenge is about camp sites and stops. Places that
# are buildings to stay in are merged too, but never become its tasks.
NOT_CAMP_SITES = ROOFED_KINDS
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

# Evidence a value needs before it becomes a task.
WINDOW_DAYS = 90
MIN_TRAVELLERS = 2
MIN_LEAD = 2  # confirming sources minus doubting sources

# Marks a doubt in the evidence: a traveller said the fact is wrong without
# giving a value.
DOUBT = "doubt"

# How a mapper can check a value, by fact. Facts not listed use CHECK_DEFAULT.
CHECK_ROAD = "a survey, or street-level photos (Mapillary or Panoramax) of the access road"
CHECK_SIGN = "the operator's own sign or website"
CHECK_SEEN = "a survey, street-level photos or recent imagery"
HOW_TO_CHECK = {
    "access": CHECK_ROAD,
    "name": "the signage at the place, or a survey",
    "status": "a survey, recent street-level photos, or the operator's website",
    "fee": CHECK_SIGN,
    "opening_hours": CHECK_SIGN,
    **{field: CHECK_SIGN for field in CONTACT_FACTS},
    "toilets": CHECK_SEEN,
    "drinking_water": CHECK_SEEN,
    "water_type": CHECK_SEEN,
    "showers": CHECK_SEEN,
    "dump_station": CHECK_SEEN,
}
CHECK_DEFAULT = "a survey, or the operator's own sign or website"

# Words that only say what kind of place it is. A name made of nothing else
# is a description, not a name, and is never sent.
GENERIC_WORDS = frozenset("""
    a an and the of for at on by only
    rest area areas stop stops stopping stopover stopovers roadside road side layby bay bays
    heavy vehicle vehicles truck trucks truckie truckies long parking park parks car carpark
    camp camps campground campgrounds camping campsite campsites site sites caravan caravans
    rv motorhome motorhomes overnight free 24 hour hours
    dump point points station toilet toilets amenities picnic reserve lookout showground
    public place spot
""".split())
WORD_RE = re.compile(r"[a-z0-9]+")


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


def sendable(field: str, value, source: str | None = None) -> bool:
    """False for a text value holding an email address, phone number or link.

    Also False for an opening_hours value from feedback that carries a
    quoted comment: that is free text no person has read, so it never goes
    to OSM mappers. A verified owner's comment is sent.
    """
    if not isinstance(value, str) or field in CONTACT_FACTS:
        return True
    if field == "opening_hours" and source != "owner" and '"' in value:
        return False
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


def double_mapping_tasks(places: dict, extract: dict, geometry: Geometry | None = None) -> list[dict]:
    geometry = geometry or Geometry()
    tasks = []
    for record in places.values():
        if record.get("status") == "merged" or not record.get("osm") or not record.get("merged_osm"):
            continue
        if record["kind"] in NOT_CAMP_SITES or from_community_report(record):
            continue
        own = osm_ref(record["osm"])
        refs = [own] + [osm_ref(m) for m in record["merged_osm"] if not m.get("part")]
        present = [ref for ref in refs if ref in extract]
        if own not in present:
            continue
        # Never a task for two places the rules find separate (pipeline.geometry).
        present = [own] + [
            ref for ref in present[1:]
            if not judge(extract[own], extract[ref], geometry, MAX_DISTANCE_M).separate
        ]
        if len(present) < 2:
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


def descriptive_name(name: str, kind: str | None = None) -> bool:
    """True for a name that only says what kind of place it is, such as "Heavy Vehicle Rest Area"."""
    plain = " ".join(WORD_RE.findall(name.lower()))
    labels = {" ".join(WORD_RE.findall(label.lower())) for label in KIND_LABEL.values()}
    if kind in KIND_LABEL:
        labels.add(" ".join(WORD_RE.findall(KIND_LABEL[kind].lower())))
    return plain in labels or all(word in GENERIC_WORDS for word in plain.split())


def parse_time(text: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def australian_date(when: datetime) -> str:
    """A day as Australians write it, such as 4 November 2026, in Sydney time."""
    try:
        from zoneinfo import ZoneInfo

        when = when.astimezone(ZoneInfo("Australia/Sydney"))
    except Exception:  # no time zone data: the UTC day is close enough
        when = when.astimezone(timezone.utc)
    return f"{when.day} {when:%B %Y}"


def resolve_place(place_id, places: dict) -> str | None:
    """The record a place id now lives in, following merges."""
    seen = set()
    while place_id in places and places[place_id].get("status") == "merged" and place_id not in seen:
        seen.add(place_id)
        place_id = places[place_id].get("merged_into")
    return place_id if place_id in places else None


def item_claims(item: dict) -> list[tuple[str, str, object]]:
    """(source, key, value) for each value an applied inbox item stated. Key is a fact, "name" or "status".

    A doubt gives (DOUBT, fact, None): it says the fact is wrong, with no value.
    """
    kind = item.get("type")
    if kind == "report":
        report = item.get("report") or {}
        if report.get("kind") == DOUBT and report.get("fact"):
            return [(DOUBT, report["fact"], None)]
        if report.get("kind") == "fact" and report.get("fact"):
            return [("feedback", report["fact"], report.get("value"))]
        if report.get("kind") == "name":
            return [("feedback", "name", report.get("value"))]
        if report.get("kind") == "closed":
            return [("feedback", "status", "closed")]
    elif kind == "check" and (item.get("check") or {}).get("still_there") is False:
        return [("feedback", "status", "closed")]
    elif kind == "owner_update":
        update = item.get("update") or {}
        claims = [("owner", fact, value) for fact, value in (update.get("facts") or {}).items()]
        if update.get("name"):
            claims.append(("owner", "name", update["name"]))
        return claims
    return []


def load_evidence(inbox: Path, places: dict) -> dict[tuple[str, str], list[dict]]:
    """(place id, key) -> what each applied inbox item stated about it: source, value, who and when.

    Only items whose outcome is "applied" count, except doubts: a doubt
    counts whatever its outcome, unless it was refused, because one held for
    a reviewer still says the fact is in question. "who" stays in memory to
    count independent sources and is never written out.
    """
    applied = Path(inbox) / "applied"
    if not applied.is_dir():
        raise SystemExit(f"{inbox} has no applied/ folder: is it a checkout of the inbox?")
    evidence: dict[tuple[str, str], list[dict]] = {}
    for path in sorted(applied.rglob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"skipping {path}: not valid JSON", file=sys.stderr)
            continue
        if not isinstance(item, dict):
            continue
        result = (item.get("outcome") or {}).get("result")
        doubt = item.get("type") == "report" and (item.get("report") or {}).get("kind") == DOUBT
        if result != "applied" and not (doubt and result != "refused"):
            continue
        place = resolve_place(item.get("place"), places)
        received = parse_time(item.get("received"))
        if place is None or received is None:
            continue
        who = item.get("origin") or item.get("device")
        for source, key, value in item_claims(item):
            evidence.setdefault((place, key), []).append(
                {"source": source, "value": value, "who": who, "received": received}
            )
    return evidence


def backing(record: dict, key: str, value, source: str, evidence: dict, now: datetime) -> tuple[int, datetime] | None:
    """(independent sources, latest date) behind a value, or None when the evidence is not enough.

    An owner value counts as one source, dated when it was set, and doubts
    do not change it. A feedback value needs MIN_TRAVELLERS confirming
    sources, and at least MIN_LEAD more of them than sources that doubted
    the fact, all within WINDOW_DAYS.
    """
    found = [e for e in evidence.get((record["id"], key), []) if e["source"] == source and e["value"] == value]
    if source == "owner":
        if not record.get("owner_verified") or not found:
            return None
        return 1, parse_time(record.get("fact_dates", {}).get(key)) or max(e["received"] for e in found)
    since = now - timedelta(days=WINDOW_DAYS)
    recent = [e for e in found if since <= e["received"] <= now and e["who"]]
    count = len({e["who"] for e in recent})
    doubts = len({
        e["who"] for e in evidence.get((record["id"], key), [])
        if e["source"] == DOUBT and since <= e["received"] <= now and e["who"]
    })
    if count < MIN_TRAVELLERS or count - doubts < MIN_LEAD:
        return None
    return count, max(e["received"] for e in recent)


def change_line(source: str, backed: tuple[int, datetime], text: str, how: str) -> str:
    """The task's line for one value, with who stated it and when, then how to check it."""
    count, when = backed
    if source == "owner":
        line = f"- The verified owner says {text}, set on {australian_date(when)}."
    else:
        line = f"- {count} travellers reported {text}, most recently on {australian_date(when)}."
    return f"{line}\n  - Check it with {how}."


def report_changes(record: dict, osm_place: dict, evidence: dict, now: datetime) -> list[str]:
    """A line for each value from feedback or the owner that OSM does not show yet and the inbox backs."""
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
        if not tag or not sendable(field, value, source):
            continue
        backed = backing(record, field, value, source, evidence, now)
        if backed:
            changes.append(change_line(source, backed, f"{describe(field, value)} ({tag})", HOW_TO_CHECK.get(field, CHECK_DEFAULT)))
    name = record.get("name")
    source = sources.get("name")
    if (
        name and name != osm_place.get("name") and source in REPORT_SOURCES
        and sendable("name", name) and not descriptive_name(name, record.get("kind"))
    ):
        backed = backing(record, "name", name, source, evidence, now)
        if backed:
            changes.append(change_line(source, backed, f"the name is {name} (name={name})", HOW_TO_CHECK["name"]))
    source = sources.get("status")
    if record.get("status") == "closed" and source in REPORT_SOURCES:
        backed = backing(record, "status", "closed", source, evidence, now)
        if backed:
            text = "it has closed for good (no single tag says this, so edit as you see fit)"
            changes.append(change_line(source, backed, text, HOW_TO_CHECK["status"]))
    return changes


def traveller_report_tasks(places: dict, extract: dict, evidence: dict, now: datetime) -> list[dict]:
    tasks = []
    for record in places.values():
        if record.get("status") == "merged" or not record.get("osm") or from_community_report(record):
            continue
        ref = osm_ref(record["osm"])
        if ref not in extract:
            continue
        changes = report_changes(record, extract[ref], evidence, now)
        if not changes:
            continue
        properties = {
            "name": record.get("name") or "Unnamed place",
            "changes": "\n".join(changes),
            "ocdb_id": record["id"],
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


NO_INBOX = "no --inbox given: report tasks need the inbox's evidence, so the file has no tasks"


def write_challenges(
    root: Path,
    extract: list[dict],
    inbox: Path | None = None,
    now: datetime | None = None,
    geometry: Geometry | None = None,
) -> dict[str, dict]:
    """Write both challenge files. Returns, per file, the task count and the ids added and removed.

    Without an inbox the report file is written with no tasks, and its entry
    says why under "skipped".
    """
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    places = load_places(root)
    by_ref = {osm_ref(place["osm"]): place for place in extract}
    reports = traveller_report_tasks(places, by_ref, load_evidence(inbox, places), now) if inbox else []
    files = {
        DOUBLE_MAPPING: double_mapping_tasks(places, by_ref, geometry),
        TRAVELLER_REPORTS: reports,
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
    if not inbox:
        result[TRAVELLER_REPORTS]["skipped"] = NO_INBOX
    return result


TITLES = {DOUBLE_MAPPING: "Double mapping", TRAVELLER_REPORTS: "Traveller reports"}


def render_report(result: dict[str, dict]) -> str:
    lines = ["", "## MapRoulette", ""]
    for name, change in result.items():
        lines.append(
            f"- {TITLES[name]} (`maproulette/{name}`): {change['count']} task(s), "
            f"{len(change['added'])} new, {len(change['removed'])} gone."
        )
        if change.get("skipped"):
            lines.append(f"  - Not built: {change['skipped']}.")
        for word, items in (("New", change["added"]), ("Gone", change["removed"])):
            for place_id, place_name in items:
                lines.append(f"  - {word}: {escape_cell(place_name)} ({place_id})")
    lines += ["", "Merging this PR approves these tasks. Then rebuild the challenges in MapRoulette (see maproulette/README.md).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("extract", type=Path, help="NDJSON written by pipeline.extract")
    parser.add_argument("--root", type=Path, default=Path("."), help="data repository root")
    parser.add_argument("--report", type=Path, help="sync report to append the MapRoulette section to")
    parser.add_argument("--inbox", type=Path, help="checkout of the feedback inbox (its applied/ folder), the evidence for report tasks")
    parser.add_argument("--geometry", type=Path, help="geometry sidecar written by pipeline.extract --geometry, for the double mapping rules")
    args = parser.parse_args(argv)
    result = write_challenges(args.root, load_extract(args.extract), inbox=args.inbox, geometry=load_geometry(args.geometry))
    for name, change in result.items():
        print(f"{name}: {change['count']} task(s), {len(change['added'])} new, {len(change['removed'])} gone", file=sys.stderr)
        if change.get("skipped"):
            print(f"{name}: {change['skipped']}", file=sys.stderr)
    if args.report:
        with open(args.report, "a", encoding="utf-8", newline="\n") as f:
            f.write(render_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
