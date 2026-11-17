"""A label for each unnamed place to stay, built by the release.

A label describes a place that has no name of its own from the OSM objects
around it, such as "Campground by Black Bobs Creek". It is a description,
not a name: Open Camping DB never makes up a name. It is only in released
records (places.ndjson.gz and ocdb.sqlite.gz), never in places/, as
{"text": ..., "from": ...}, where from is the fact the text came from.
"""

import math
import re

from .common import KIND_LABEL
from .duplicates import has_name
from .states import StateLookup

# A waterway whose name ends in River is "on the" river, unless the name
# starts with The, which would read "on the The ...".
RIVER = re.compile(r"\briver$", re.IGNORECASE)
STARTS_WITH_THE = re.compile(r"^the\b", re.IGNORECASE)
# nearest_feature types a place is "by". A beach is "at", anything else "near".
WET = {"water", "bay", "spring", "waterfall", "wetland"}
BRACKETS = re.compile(r"\s*\([^)]*\)")
# The eight points of the compass, clockwise from north.
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
# Mean earth radius, as pipeline.localities measures distance_m.
EARTH_RADIUS_M = 6_371_008.8


def kind_label(kind: str) -> str:
    """The kind's display label from schema/kinds.json, in sentence case."""
    label = KIND_LABEL.get(kind, kind.replace("_", " "))
    return label[:1].upper() + label[1:].lower()


def park_name(name: str) -> str:
    """A protected area's name without bracketed notes such as "(IUCN Cat. VI)"."""
    return " ".join(BRACKETS.sub("", name).split())


def describe(facts: dict) -> tuple[str, str] | None:
    """(text after the kind label, fact it came from) for a place's facts, or None."""
    waterway = facts.get("nearest_waterway")
    if waterway:
        name = waterway["name"].strip()
        word = "on the" if RIVER.search(name) and not STARTS_WITH_THE.search(name) else "by"
        return f"{word} {name}", "nearest_waterway"
    near = facts.get("nearest_feature")
    if near:
        word = "at" if near["type"] == "beach" else "by" if near["type"] in WET else "near"
        return f"{word} {near['name'].strip()}", "nearest_feature"
    park = park_name(facts.get("protected_area", {}).get("name", ""))
    if park:
        return f"in {park}", "protected_area"
    return None


def distance_m(a: dict, b: dict) -> float:
    """Great circle distance between two {"lat", "lon"} points, in metres."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def compass_point(frm: dict, to: dict) -> str:
    """The point of the eight-point compass from frm towards to."""
    lat1, lon1, lat2, lon2 = map(math.radians, (frm["lat"], frm["lon"], to["lat"], to["lon"]))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    degrees = math.degrees(math.atan2(y, x)) % 360
    return COMPASS[int((degrees + 22.5) // 45) % 8]


def how_far(metres: float) -> str:
    """A distance rounded to 100 m, at least 100 m: "800 m", "1.8 km"."""
    rounded = max(100, round(metres / 100) * 100)
    if rounded < 1000:
        return f"{rounded} m"
    return f"{rounded / 1000:.1f} km"


def town_of(record: dict, by_id: dict[int, dict], localities: list[dict]) -> dict | None:
    """The locality row a label measures from: the record's locality fact, else the nearest locality."""
    osm_id = (record.get("facts", {}).get("locality") or {}).get("osm_id")
    if osm_id in by_id:
        return by_id[osm_id]
    if not localities:
        return None
    return min(localities, key=lambda row: (distance_m(record["location"], row), row["osm_id"]))


def repeated(texts: dict[str, str], state_of: dict[str, str | None], ids: list[str]) -> list[str]:
    """The ids among ids whose text another of them in the same state shares."""
    count: dict[tuple, int] = {}
    for place_id in ids:
        key = (state_of[place_id], texts[place_id])
        count[key] = count.get(key, 0) + 1
    return [place_id for place_id in ids if count[(state_of[place_id], texts[place_id])] > 1]


def labels_for(
    records: list[dict], states: StateLookup | None = None, localities: list[dict] | None = None
) -> dict[str, dict]:
    """The label of each record that gets one, by id.

    When two or more labelled places in the same state (pipeline.states) share
    a text, each one adds ", " and the name of its locality fact, unless the
    text already holds that name. One with no locality keeps the plain text.

    Places that still share a text then say where they are instead: ", " and
    the distance and compass point from their locality fact's town (or the
    nearest row of localities when they have no locality fact), to 100 m, as
    in "Reserve campsite on the Murray River, 1.8 km NE of Merbein". Places
    still alike are almost always within about 100 m of each other, such as
    the cabins of one lodge, and keep that text. localities is the rows of
    localities/localities.ndjson; without them this step is left out.
    """
    found: dict[str, dict] = {}
    base: dict[str, str] = {}
    labelled = []
    for record in records:
        if record.get("group") != "overnight" or has_name(record):
            continue
        described = describe(record.get("facts", {}))
        if described is not None:
            text, source = described
            base[record["id"]] = f"{kind_label(record['kind'])} {text}"
            found[record["id"]] = {"text": base[record["id"]], "from": source}
            labelled.append(record)
    if not labelled:
        return found
    states = states or StateLookup()
    in_state = states.states_of([(r["location"]["lon"], r["location"]["lat"]) for r in labelled])
    state_of = {record["id"]: state for record, state in zip(labelled, in_state)}
    by_record = {record["id"]: record for record in labelled}
    texts = {place_id: label["text"] for place_id, label in found.items()}
    for place_id in repeated(texts, state_of, list(texts)):
        town = (by_record[place_id]["facts"].get("locality") or {}).get("name", "").strip()
        if town and town not in texts[place_id]:
            texts[place_id] = f"{texts[place_id]}, {town}"
    by_id = {row["osm_id"]: row for row in localities or []}
    for place_id in repeated(texts, state_of, list(texts)):
        record = by_record[place_id]
        town = town_of(record, by_id, localities or [])
        if town is not None:
            where = f"{how_far(distance_m(town, record['location']))} {compass_point(town, record['location'])}"
            texts[place_id] = f"{base[place_id]}, {where} of {town['name']}"
    for place_id, text in texts.items():
        found[place_id]["text"] = text
    return found


def with_label(record: dict, label: dict | None) -> dict:
    """The record with its label, or the record itself when it has none. The record passed in is not changed."""
    if label is None:
        return record
    return {**record, "label": label}
