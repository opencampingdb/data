"""A label for each unnamed place to stay, built by the release.

A label describes a place that has no name of its own from the OSM objects
around it, such as "Campground by Black Bobs Creek". It is a description,
not a name: Open Camping DB never makes up a name. It is only in released
records (places.ndjson.gz and ocdb.sqlite.gz), never in places/, as
{"text": ..., "from": ...}, where from is the fact the text came from.
"""

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


def labels_for(records: list[dict], states: StateLookup | None = None) -> dict[str, dict]:
    """The label of each record that gets one, by id.

    When two or more labelled places in the same state (pipeline.states) share
    a text, each one adds ", " and the name of its locality fact, unless the
    text already holds that name. One with no locality keeps the plain text.
    """
    found: dict[str, dict] = {}
    labelled = []
    for record in records:
        if record.get("group") != "overnight" or has_name(record):
            continue
        described = describe(record.get("facts", {}))
        if described is not None:
            text, source = described
            found[record["id"]] = {"text": f"{kind_label(record['kind'])} {text}", "from": source}
            labelled.append(record)
    if not labelled:
        return found
    states = states or StateLookup()
    in_state = states.states_of([(r["location"]["lon"], r["location"]["lat"]) for r in labelled])
    same: dict[tuple, list[dict]] = {}
    for record, state in zip(labelled, in_state):
        same.setdefault((state, found[record["id"]]["text"]), []).append(record)
    for group in same.values():
        if len(group) < 2:
            continue
        for record in group:
            label = found[record["id"]]
            town = (record["facts"].get("locality") or {}).get("name", "").strip()
            if town and town not in label["text"]:
                label["text"] = f"{label['text']}, {town}"
    return found


def with_label(record: dict, label: dict | None) -> dict:
    """The record with its label, or the record itself when it has none. The record passed in is not changed."""
    if label is None:
        return record
    return {**record, "label": label}
